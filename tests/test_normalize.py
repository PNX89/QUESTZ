from __future__ import annotations

import pytest

from questz.normalize import diff, matches, normalize, parse, walk
from questz.types import ContractError

CONTAINER = '[data-testid="items"]'

COSMETIC = [
    "items.attr-order.html",
    "items.class-churn.html",
    "items.whitespace.html",
    "items.extra-rows.html",
    "items.framework-ids.html",
    "items.analytics-script.html",
]

SELECTOR_DOC = """
<div id="items" class="wrap">
  <table>
    <tr class="row" data-testid="item-row"><td class="price" data-testid="price">1</td></tr>
    <tr class="row" data-testid="other"><td class="price">2</td></tr>
  </table>
  <p class="price">outside</p>
</div>
"""


@pytest.mark.parametrize("variant", COSMETIC)
def test_cosmetic_variants_leave_the_signature_unchanged(testsite_html, variant):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    observed = normalize(testsite_html(f"v1c/{variant}"), container=CONTAINER)
    assert observed.lines == baseline.lines
    assert observed.signature == baseline.signature


def test_adding_a_row_keeps_the_signature_and_changes_the_counts(testsite_html):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    observed = normalize(testsite_html("v1c/items.extra-rows.html"), container=CONTAINER)
    assert observed.signature == baseline.signature
    assert [count for _, count in baseline.counts] == [12]
    assert [count for _, count in observed.counts] == [13]


def test_a_collapsed_run_keeps_every_block_s_nested_counts():
    """Run equality is tested on lines, and a nested run's length lives only in the counts,
    so keeping the first block's counts alone made two product groups with different item
    counts indistinguishable in every output the tool produces."""

    def groups(*lengths: int):
        body = "".join(f"<section><ul>{'<li></li>' * n}</ul></section>" for n in lengths)
        return normalize(f'<div data-testid="c">{body}</div>', container='[data-testid="c"]')

    same, diverged = groups(3, 3), groups(3, 9)
    assert same.lines == diverged.lines
    assert [count for _, count in same.counts] == [3, 3, 2]
    assert [count for _, count in diverged.counts] == [3, 9, 2]


def test_comments_and_scripts_are_discarded():
    plain = normalize('<div data-testid="c"><p></p></div>', container='[data-testid="c"]')
    noisy = normalize(
        '<div data-testid="c"><!-- tag --><script>var x = 1;</script>'
        "<noscript><img src='p.gif'></noscript><p></p></div>",
        container='[data-testid="c"]',
    )
    assert noisy.signature == plain.signature


def test_inline_style_and_framework_ids_are_dropped():
    plain = normalize(
        '<div data-testid="c"><p role="note"></p></div>', container='[data-testid="c"]'
    )
    churned = normalize(
        '<div data-testid="c" class="css-1x2y3z"><p role="note" id=":r7:"'
        ' style="--gap:4px" _ngcontent-qzx-c12=""></p></div>',
        container='[data-testid="c"]',
    )
    assert churned.signature == plain.signature


def test_a_generated_attribute_value_collapses_to_a_star():
    structure = normalize(
        '<div data-testid="c"><p data-testid="row-9f8e7d6c5b4a3928374655647382910a"></p></div>',
        container='[data-testid="c"]',
    )
    assert structure.lines[1] == "1|p|data-testid=*"
    # `_STABLE_VALUE` is anchored `^...$`, and Python's `$` also matches just before a
    # trailing newline, so a naive `.match` let a value through the pattern does not
    # describe. A survived value goes straight into a `depth|tag|attrs` line, and this one
    # would put a newline inside what the diff and the report both treat as a single line.
    newline_terminated = normalize(
        '<div data-testid="c"><p role="note\n"></p></div>',
        container='[data-testid="c"]',
    )
    assert newline_terminated.lines[1] == "1|p|role=*"
    assert "\n" not in newline_terminated.lines[1]


def test_renaming_a_testid_changes_the_signature(testsite_html):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    renamed = normalize(
        testsite_html("v1/items.html").replace('data-testid="price"', 'data-testid="cost"'),
        container=CONTAINER,
    )
    assert renamed.signature != baseline.signature


def test_moving_a_node_to_a_different_parent_changes_the_signature():
    before = normalize(
        '<div data-testid="c"><section><span role="note"></span></section><footer></footer></div>',
        container='[data-testid="c"]',
    )
    after = normalize(
        '<div data-testid="c"><section></section><footer><span role="note"></span></footer></div>',
        container='[data-testid="c"]',
    )
    assert after.signature != before.signature


def test_diff_names_the_changed_node(testsite_html):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    observed = normalize(testsite_html("v2/items.html"), container=CONTAINER)
    lines = diff(baseline.lines, observed.lines)
    assert "-4|td|data-testid=price" in lines
    assert "+4|td|data-testid=cost" in lines


def test_diff_is_empty_when_the_lines_match(testsite_html):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    assert diff(baseline.lines, baseline.lines) == ()


def test_container_scoping_ignores_changes_outside_the_container(testsite_html):
    baseline = normalize(testsite_html("v1/items.html"), container=CONTAINER)
    changed = testsite_html("v1/items.html").replace(
        '<header data-testid="site-header">',
        '<header data-testid="site-header"><aside role="banner"><b></b></aside>',
    )
    assert normalize(changed, container=CONTAINER).signature == baseline.signature
    assert normalize(changed).signature != normalize(testsite_html("v1/items.html")).signature


def test_an_absent_container_normalizes_to_an_empty_structure(testsite_html):
    structure = normalize(testsite_html("v2i/items.html"), container=CONTAINER)
    assert structure.lines == ()
    assert structure.counts == ()


def test_void_elements_do_not_swallow_their_siblings():
    structure = normalize(
        '<div data-testid="c"><br><img alt=""><p role="note"></p></div>',
        container='[data-testid="c"]',
    )
    assert structure.lines == ("0|div|data-testid=c", "1|br|", "1|img|", "1|p|role=note")


def test_unclosed_cells_stay_siblings_instead_of_nesting():
    """`html.parser` implements no implied end tags, so without this a page that omits
    </td>, which real portals do constantly, nests every cell inside the first one and the
    whole row's text collapses into it as one unparseable price."""
    structure = normalize(
        '<table data-testid="t"><tr><td data-testid="a">1<td data-testid="b">2</table>',
        container='[data-testid="t"]',
    )
    assert structure.lines == (
        "0|table|data-testid=t",
        "1|tr|",
        "2|td|data-testid=a",
        "2|td|data-testid=b",
    )
    cells = matches('<table><tr><td class="price">19.99<td class="stock">low</table>', "td")
    assert [cell.text for cell in cells] == ["19.99", "low"]


def test_an_unclosed_row_is_closed_by_the_next_one():
    structure = normalize(
        '<table data-testid="t"><tr><td role="a">1<tr><td role="b">2</table>',
        container='[data-testid="t"]',
    )
    assert structure.lines == (
        "0|table|data-testid=t",
        "1|tr|",
        "2|td|role=a",
        "1|tr|",
        "2|td|role=b",
    )


def test_an_unclosed_list_item_closes_at_the_next_one_but_not_through_a_nested_list():
    flat = normalize('<ul data-testid="c"><li>a<li>b</ul>', container='[data-testid="c"]')
    # Two identical siblings, so they collapse to one run marked x* with the count kept.
    assert flat.lines == ("0|ul|data-testid=c", "1|li||x*")
    assert [count for _, count in flat.counts] == [2]
    nested = normalize(
        '<ul data-testid="c"><li>a<ul><li role="inner">b</li></ul></li></ul>',
        container='[data-testid="c"]',
    )
    assert nested.lines == ("0|ul|data-testid=c", "1|li|", "2|ul|", "3|li|role=inner")


def test_an_unclosed_paragraph_is_closed_by_the_block_that_follows_it():
    structure = normalize(
        '<div data-testid="c"><p>one<p>two<div role="after"></div></div>',
        container='[data-testid="c"]',
    )
    assert structure.lines == ("0|div|data-testid=c", "1|p||x*", "1|div|role=after")


def test_a_hydration_template_is_not_part_of_the_tree():
    """A template's content is an inert DocumentFragment nobody sees, but `page.content()`
    serializes it, so unskipped it reads as a pile of nodes that appeared from nowhere."""
    plain = normalize('<div data-testid="c"><p></p></div>', container='[data-testid="c"]')
    hydrated = normalize(
        '<div data-testid="c"><template id="tpl"><section role="row"><b></b></section>'
        "</template><p></p></div>",
        container='[data-testid="c"]',
    )
    assert hydrated.signature == plain.signature


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("td", 2),
        (".price", 3),
        ("#items", 1),
        ("[data-testid]", 3),
        ('[data-testid="price"]', 1),
        ("[data-testid=price]", 1),
        ('tr.row[data-testid="item-row"]', 1),
        ("table td", 2),
        ("div > p", 1),
    ],
)
def test_the_supported_css_subset(selector, expected):
    assert len(matches(SELECTOR_DOC, selector)) == expected


def test_a_descendant_combinator_is_not_a_child_combinator():
    assert len(matches(SELECTOR_DOC, "div td")) == 2
    assert len(matches(SELECTOR_DOC, "div > td")) == 0


@pytest.mark.parametrize(
    "selector",
    [
        "div, span",
        "div:hover",
        "*",
        "div + p",
        "div ~ p",
        'td[data-testid^="pri"]',
        "div >",
        # An attribute with no name at all. The operator check above it used to be a substring
        # test, and "" is a substring of every string, so this reached the error message and
        # indexed an empty string. An IndexError is not a ContractError: it escapes cli.main
        # and exits 1, and 1 is the code this tool uses to say the site changed.
        '[="items-table"]',
        "[]",
    ],
)
def test_unsupported_selector_syntax_raises_and_names_the_fragment(selector):
    with pytest.raises(ContractError) as caught:
        matches(SELECTOR_DOC, selector)
    assert selector in str(caught.value)


def test_matched_elements_carry_their_collapsed_text(testsite_html):
    found = matches(
        testsite_html("v1/items.html"), 'tr[data-testid="item-row"] td[data-testid="price"]'
    )
    assert [element.text for element in found][:2] == ["€19.99", "€42.50"]


@pytest.mark.parametrize(
    ("markup", "reads_as"),
    [
        # The one that matters: a price with a styled group separator, which is how a real
        # storefront marks up a number and exactly what this tool is pointed at.
        ("<td>1,2<span>34</span>.56 EUR</td>", "1,234.56 EUR"),
        # Child first, own text after.
        ("<td><b>EUR</b> 99.50</td>", "EUR 99.50"),
        # Alternating, which is where a single append-self-then-descendants pass goes worst.
        ("<td>a<i>b</i>c<i>d</i>e</td>", "abcde"),
        # Nested two deep, so the interleaving has to be recursive rather than one level.
        ("<td>x<span>y<b>z</b>w</span>v</td>", "xyzwv"),
    ],
)
def test_element_text_is_in_document_order(markup: str, reads_as: str) -> None:
    """`Element.text` returned a SCRAMBLED value, which is this repository's own failure mode.

    It walked self first and then every descendant, so an element's own text always preceded its
    children's whatever the markup said. `<td>1,2<span>34</span>.56 EUR</td>` reads as
    `1,234.56 EUR` to a human and came back as `1,2.56 EUR34`.

    Both directions of that are bad and the second is worse. A canary comparing the scramble to
    an expected value reports the site as changed, which wastes a morning. A canary comparing two
    scrambles of the same cell sees no difference and passes a wrong number through, which is the
    silent-wrong-data failure the whole repository exists to stop.

    Nothing caught it because every fixture in the suite put an element's text either wholly
    before or wholly after its children, so the traversal order and the document order agreed.
    The cases below are chosen so that they disagree.
    """
    root = parse(f"<table><tr>{markup}</tr></table>")
    cell = next(node for node in walk(root) if node.tag == "td")
    assert cell.text == reads_as
