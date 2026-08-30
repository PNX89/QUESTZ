"""HTML tree parsing, a structural serialization that ignores cosmetic churn, and a
small CSS selector subset shared by the browserless and the browser paths."""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser

from questz.types import ContractError

__all__ = [
    "ATTR_ALLOWLIST",
    "Element",
    "Structure",
    "diff",
    "matches",
    "normalize",
    "normalize_tree",
    "parse",
    "select",
    "walk",
]

ATTR_ALLOWLIST: frozenset[str] = frozenset({"role", "type", "name", "data-testid", "aria-label"})

# An attribute value survives only when it looks hand written. Build hashed values
# (Tailwind JIT classes, styled-components ids, `:r7:` React ids) collapse to "*" so the
# anchor is kept and the churn is not.
_STABLE_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

_VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)  # fmt: skip
# `template` content is an inert DocumentFragment, not part of the rendered tree, so a
# hydration payload is markup the user never sees. `page.content()` serializes it anyway.
_SKIPPED_SUBTREES = frozenset({"script", "style", "svg", "noscript", "template"})

# Start tags that imply the end of an open element, with the ancestors the search stops at.
# `html.parser` implements none of this, and portal markup leaves <td>, <tr> and <li>
# unclosed constantly: without it the whole table's text collapses into the first cell and
# the canary reports a price formatting problem that does not exist.
_IMPLIED_END: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol", "menu", "table", "td", "th"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl", "table", "td", "th"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl", "table", "td", "th"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "tr": (frozenset({"tr"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist", "optgroup"})),
}
# The HTML5 list of start tags that close an open <p>, and the containers that would have
# closed it already.
_P = frozenset({"p"})
_CLOSES_P = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
    }
)  # fmt: skip
_BLOCK_SCOPE = frozenset(
    {
        "article", "aside", "blockquote", "body", "dd", "details", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "li", "main", "nav", "ol", "section", "table", "tbody", "td",
        "tfoot", "th", "thead", "tr", "ul",
    }
)  # fmt: skip

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_UNSUPPORTED_SYNTAX = {
    ",": "selector lists",
    "*": "the universal selector",
    ":": "pseudo classes",
    "+": "the adjacent sibling combinator",
    "~": "the general sibling combinator",
}


@dataclass(slots=True)
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Element] = field(default_factory=list)
    parent: Element | None = field(default=None, repr=False, compare=False)
    order: int = -1
    #: Each own-text run, paired with the number of children already present when it arrived.
    #: The pairing is what puts the text back in document order, and without it the order is
    #: whatever the traversal happens to be.
    text_parts: list[tuple[int, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Own and descendant text, in DOCUMENT ORDER, with whitespace collapsed to single spaces.

        THIS RETURNED A SCRAMBLED VALUE, and it is the defect this repository exists to catch.
        It walked self first and then every descendant, so an element's own text always came
        before its children's whatever the markup said. On a perfectly ordinary price cell:

            <td>1,2<span>34</span>.56 EUR</td>

        a human reads `1,234.56 EUR` and this returned `1,2.56 EUR34`. A canary comparing that to
        an expected value sees a difference and reports the site as changed; a canary comparing
        two scrambles of the same cell sees none and passes a wrong number through. Both are the
        silent-wrong-data failure the whole repository is about.

        Fixed by recording, with each own-text run, how many children were already there when it
        arrived, then interleaving on the way out. That is the smallest change that restores
        document order without a second parse.
        """
        return " ".join("".join(_in_order(self)).split())


def _in_order(element: Element) -> list[str]:
    """One element's text and its descendants', interleaved as the document has them.

    Own-text runs carry the number of children that existed when they were read, so a run
    recorded at position 2 belongs after the second child's subtree and before the third's.
    """
    parts: list[str] = []
    by_position: dict[int, list[str]] = {}
    for position, data in element.text_parts:
        by_position.setdefault(position, []).append(data)

    parts.extend(by_position.get(0, []))
    for index, child in enumerate(element.children, start=1):
        parts.extend(_in_order(child))
        parts.extend(by_position.get(index, []))
    # Anything recorded past the last child, which happens when markup closes oddly.
    for position in sorted(p for p in by_position if p > len(element.children)):
        parts.extend(by_position[position])
    return parts


@dataclass(frozen=True, slots=True)
class Structure:
    lines: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    signature: str


def walk(root: Element, *, include_self: bool = False) -> Iterator[Element]:
    """Depth first, document order."""
    stack: list[Element] = [root] if include_self else list(reversed(root.children))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document")
        self._stack: list[Element] = [self.root]
        self._skipped: str | None = None
        self._skip_depth = 0
        self._order = 0

    def _close_implied(self, tag: str) -> None:
        """Close what this start tag implies is over, stopping at a container boundary.

        Deleting the whole tail of the stack is what makes `<li><p>a<li>` close the
        paragraph as well as the item, which is what a browser does.
        """
        if tag in _IMPLIED_END:
            closes, boundary = _IMPLIED_END[tag]
        elif tag in _CLOSES_P:
            closes, boundary = _P, _BLOCK_SCOPE
        else:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            current = self._stack[index].tag
            if current in closes:
                del self._stack[index:]
                return
            if current in boundary:
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipped is not None:
            if tag == self._skipped:
                self._skip_depth += 1
            return
        if tag in _SKIPPED_SUBTREES:
            self._skipped = tag
            self._skip_depth = 1
            return
        self._close_implied(tag)
        parent = self._stack[-1]
        element = Element(
            tag=tag,
            attrs={name: value or "" for name, value in attrs},
            parent=parent,
            order=self._order,
        )
        self._order += 1
        parent.children.append(element)
        if tag not in _VOID_ELEMENTS:
            self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        if self._skipped is not None:
            if tag == self._skipped:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skipped = None
            return
        if tag in _VOID_ELEMENTS:
            return
        # Closing an ancestor implicitly closes everything below it, which is how unclosed
        # <td> and <li> in real markup end up in the right place.
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._skipped is None:
            current = self._stack[-1]
            # The child count AT THIS MOMENT is the text's position among its siblings.
            current.text_parts.append((len(current.children), data))


def parse(html: str) -> Element:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _kept_attrs(element: Element) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    for name in sorted(element.attrs):
        if name not in ATTR_ALLOWLIST:
            continue
        value = element.attrs[name]
        kept.append((name, value if _STABLE_VALUE.match(value) else "*"))
    return kept


def _attr_text(element: Element) -> str:
    return ",".join(f"{name}={value}" for name, value in _kept_attrs(element))


def _node_key(element: Element) -> str:
    attrs = _attr_text(element)
    return f"{element.tag}[{attrs}]" if attrs else element.tag


def _subtree(
    element: Element, depth: int, parent_path: str
) -> tuple[list[str], list[tuple[str, int]]]:
    key = _node_key(element)
    node_path = f"{parent_path}>{key}" if parent_path else key
    child_lines, child_counts = _sibling_run(element.children, depth + 1, node_path)
    return [f"{depth}|{element.tag}|{_attr_text(element)}", *child_lines], child_counts


def _sibling_run(
    children: list[Element], depth: int, parent_path: str
) -> tuple[list[str], list[tuple[str, int]]]:
    rendered = [_subtree(child, depth, parent_path) for child in children]
    lines: list[str] = []
    counts: list[tuple[str, int]] = []
    start = 0
    while start < len(rendered):
        block, _ = rendered[start]
        end = start + 1
        while end < len(rendered) and rendered[end][0] == block:
            end += 1
        run = end - start
        # Every block in the run, not only the first: two product groups serialize to the
        # same lines whatever their inner list lengths, so dropping the rest of the counts
        # is the one place the collapse would hide a difference completely.
        for _, block_counts in rendered[start:end]:
            counts.extend(block_counts)
        if run > 1:
            # The marker, never the number: a 12 row list and a 13 row list must serialize
            # identically or the canary trips on every list page it ever sees.
            lines.append(block[0] + "|x*")
            lines.extend(block[1:])
            key = _node_key(children[start])
            counts.append((f"{parent_path}>{key}" if parent_path else key, run))
        else:
            lines.extend(block)
        start = end
    return lines, counts


def normalize_tree(root: Element, *, container: str | None = None) -> Structure:
    if container is None:
        lines, counts = _sibling_run(root.children, 0, "")
    else:
        scope = select(root, container)
        lines, counts = _subtree(scope[0], 0, "") if scope else ([], [])
    signature = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return Structure(tuple(lines), tuple(counts), signature)


def normalize(html: str, *, container: str | None = None) -> Structure:
    return normalize_tree(parse(html), container=container)


def diff(expected: Sequence[str], observed: Sequence[str]) -> tuple[str, ...]:
    if list(expected) == list(observed):
        return ()
    return tuple(
        difflib.unified_diff(
            list(expected), list(observed), fromfile="baseline", tofile="observed", lineterm="", n=1
        )
    )


@dataclass(frozen=True, slots=True)
class _Compound:
    tag: str | None
    classes: tuple[str, ...]
    ident: str | None
    attrs: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class _Step:
    combinator: str
    compound: _Compound


def _split(selector: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    buffer: list[str] = []
    combinator = ""
    depth = 0
    quote = ""
    for char in selector:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
        elif depth:
            buffer.append(char)
            if char in "\"'":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
        elif char == "[":
            buffer.append(char)
            depth = 1
        elif char.isspace():
            if buffer:
                parts.append((combinator, "".join(buffer)))
                buffer = []
                combinator = " "
        elif char == ">":
            if buffer:
                parts.append((combinator, "".join(buffer)))
                buffer = []
            elif not parts:
                raise ContractError(f"contract selector {selector!r} starts with a combinator")
            combinator = ">"
        else:
            reason = _UNSUPPORTED_SYNTAX.get(char)
            if reason is not None:
                raise ContractError(
                    f"unsupported CSS in contract selector {selector!r}:"
                    f" {char!r} ({reason}) is outside the supported subset"
                )
            buffer.append(char)
    if depth or quote:
        raise ContractError(f"unbalanced brackets in contract selector {selector!r}")
    if buffer:
        parts.append((combinator, "".join(buffer)))
    elif combinator == ">":
        raise ContractError(f"contract selector {selector!r} ends with a combinator")
    if not parts:
        raise ContractError("contract selector is empty")
    return parts


def _parse_attr(body: str, selector: str) -> tuple[str, str | None]:
    name, sep, raw = body.partition("=")
    name = name.strip()
    if name[-1:] in "~|^$*" and sep:
        raise ContractError(
            f"unsupported attribute operator {name[-1] + '='!r} in contract selector {selector!r}"
        )
    if not _NAME.fullmatch(name):
        raise ContractError(f"unsupported attribute {body!r} in contract selector {selector!r}")
    if not sep:
        return name.lower(), None
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return name.lower(), raw


def _parse_compound(text: str, selector: str) -> _Compound:
    tag: str | None = None
    ident: str | None = None
    classes: list[str] = []
    attrs: list[tuple[str, str | None]] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char in ".#":
            match = _NAME.match(text, position + 1)
            if match is None:
                raise ContractError(
                    f"unsupported fragment {text!r} in contract selector {selector!r}"
                )
            if char == ".":
                classes.append(match.group(0))
            else:
                ident = match.group(0)
            position = match.end()
        elif char == "[":
            end = _closing_bracket(text, position, selector)
            attrs.append(_parse_attr(text[position + 1 : end], selector))
            position = end + 1
        else:
            match = _NAME.match(text, position)
            if match is None or position != 0:
                raise ContractError(
                    f"unsupported fragment {text!r} in contract selector {selector!r}"
                )
            tag = match.group(0).lower()
            position = match.end()
    if tag is None and ident is None and not classes and not attrs:
        raise ContractError(f"empty fragment in contract selector {selector!r}")
    return _Compound(tag, tuple(classes), ident, tuple(attrs))


def _closing_bracket(text: str, start: int, selector: str) -> int:
    quote = ""
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "]":
            return index
    raise ContractError(f"unbalanced brackets in contract selector {selector!r}")


@lru_cache(maxsize=256)
def _compile(selector: str) -> tuple[_Step, ...]:
    stripped = selector.strip()
    if not stripped:
        raise ContractError("contract selector is empty")
    return tuple(
        _Step(combinator, _parse_compound(text, stripped)) for combinator, text in _split(stripped)
    )


def _matches_compound(element: Element, compound: _Compound) -> bool:
    if compound.tag is not None and element.tag != compound.tag:
        return False
    if compound.ident is not None and element.attrs.get("id") != compound.ident:
        return False
    if compound.classes and not set(element.attrs.get("class", "").split()).issuperset(
        compound.classes
    ):
        return False
    for name, value in compound.attrs:
        if name not in element.attrs:
            return False
        if value is not None and element.attrs[name] != value:
            return False
    return True


def select(root: Element, selector: str) -> tuple[Element, ...]:
    steps = _compile(selector)
    current = [node for node in walk(root) if _matches_compound(node, steps[0].compound)]
    for step in steps[1:]:
        seen: set[int] = set()
        following: list[Element] = []
        for element in current:
            pool = element.children if step.combinator == ">" else walk(element)
            for candidate in pool:
                if id(candidate) in seen or not _matches_compound(candidate, step.compound):
                    continue
                seen.add(id(candidate))
                following.append(candidate)
        current = following
    return tuple(sorted(current, key=lambda element: element.order))


def matches(html: str, selector: str) -> tuple[Element, ...]:
    return select(parse(html), selector)
