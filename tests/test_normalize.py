from __future__ import annotations

import pytest

from questz.normalize import diff, matches, normalize
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
    ["div, span", "div:hover", "*", "div + p", "div ~ p", 'td[data-testid^="pri"]', "div >"],
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
