"""The only characters above ASCII allowed in this tree are ones a real page brings, and the
parser is required to handle every one of them.

A comment in `test_readme.py` said `test_every_text_file_in_the_repository_is_pure_ascii` covered
the tree. That test is in a sibling repository. Here there was no byte scan under any name.

A FLAT BAN WOULD HAVE BEEN WRONG, and running one first is how that became obvious: it reports
sixteen files, and every hit is the euro sign on a test site serving European prices. Refusing it
would mean the fixtures could no longer look like the pages this tool watches, which is the one
property those fixtures have.

So the rule is a whitelist with a job attached. Two kinds of character may appear:

    U+20AC EURO SIGN                     currency decoration, stripped by parse_decimal
    the four spaces in canary._SPACES    NBSP and friends, folded to a plain space

Both are read from the code rather than typed here, and both are exercised: the second test feeds
each declared character to `parse_decimal` and requires the right `Decimal` back. That is what
stops the whitelist becoming a place to file away whatever turned up.

Everything else is refused, and the ones that matter are the ones a person cannot see in a diff.
A right single quotation mark inside a CSS selector matches no element and reports the site as
changed. A non-breaking space inside an expected literal never equals the plain space it was
copied from. Both look correct on screen.
"""

from __future__ import annotations

import subprocess
import unicodedata
from decimal import Decimal
from pathlib import Path

import pytest

from questz.canary import _SPACES, parse_decimal

REPO = Path(__file__).resolve().parents[1]

#: Read from the translation table the parser actually applies, so adding a space character to
#: the code admits it here, and removing one from the code makes this test refuse it again.
FOLDED_SPACES = frozenset(chr(point) for point in _SPACES)

#: The one decoration character, written as an escape here and as a literal in the fixtures. The
#: fixtures need the literal, because a page that escaped every symbol would not exercise the
#: decoding path the driver takes. This file does not, and a test file carrying a copy of what it
#: polices is how the rule gets weakened by whoever tidies it next.
EURO = "\u20ac"

ALLOWED = FOLDED_SPACES | {EURO}

BINARY = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".zip"})


def committed() -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True
    )
    return [
        path
        for entry in listing.stdout.decode().split("\0")
        if entry
        for path in [REPO / entry]
        if path.is_file() and path.suffix.lower() not in BINARY
    ]


def test_the_tree_carries_no_character_this_repository_has_not_declared() -> None:
    """Bytes first: decoding would let a file that is not UTF-8 be skipped as undecodable."""
    offences, scanned = [], 0
    for path in committed():
        raw = path.read_bytes()
        scanned += 1
        if all(byte <= 0x7F for byte in raw):
            continue
        for character in dict.fromkeys(raw.decode("utf-8", "replace")):
            if not character.isascii() and character not in ALLOWED:
                offences.append(
                    f"{path.relative_to(REPO)}: U+{ord(character):04X} "
                    f"{unicodedata.name(character, 'unnamed')}"
                )
    assert scanned > 40, f"git listed {scanned} files, too few to be this tree"
    assert offences == [], offences


def test_the_declared_spaces_are_the_four_a_page_actually_uses() -> None:
    """THE TEST BELOW GETS WEAKER RATHER THAN RED IF THIS SET SHRINKS, which is why this exists.

    Reading the parametrisation from the code is right: it means adding a space character to the
    parser admits it here automatically. It is also a trap, and it was caught by trying it.
    Deleting U+202F from `_SPACES` did not turn anything red. It produced six passing tests where
    there had been seven, because the parametrised set is built from the same table. A suite that
    quietly covers one case fewer reads exactly like a suite that passed.

    So the set is pinned by name and by size. Changing the parser's table is allowed, and it has
    to come here and be argued for rather than happening in silence.
    """
    assert {"\u00a0", "\u202f", "\u2009", "\u2007"} == FOLDED_SPACES, sorted(
        f"U+{ord(character):04X} {unicodedata.name(character, 'unnamed')}"
        for character in FOLDED_SPACES
    )
    assert len(FOLDED_SPACES) == 4


@pytest.mark.parametrize("space", sorted(FOLDED_SPACES))
def test_every_space_the_whitelist_admits_is_one_the_parser_folds(space: str) -> None:
    """The whitelist has to cost something, or it is a list of excuses.

    A price with a narrow no-break space between the thousands is what a European page serves.
    If `parse_decimal` stopped folding one of these, the character would still be permitted in
    the tree by the test above while silently making a value unparseable, so the permission and
    the handling are asserted together.
    """
    assert parse_decimal(f"{EURO}1{space}234,56", decimal_separator=",") == Decimal("1234.56")


def test_the_euro_sign_is_stripped_rather_than_tolerated() -> None:
    """Decoration on both ends, which is the shape a real page uses.

    "EUR 19.99" and "19,99 EUR" are the same number, and a scraper that kept the symbol would
    hand a string to something expecting a number.
    """
    assert parse_decimal(f"{EURO}19.99") == Decimal("19.99")
    assert parse_decimal(f"19,99{EURO}", decimal_separator=",") == Decimal("19.99")
    assert parse_decimal(f"-{EURO}5.00") == Decimal("-5.00")


def test_a_character_that_looks_like_a_quote_is_refused_rather_than_folded() -> None:
    """The failure this scan exists for, made concrete rather than described.

    A right single quotation mark inside a selector is not a syntax error. It matches nothing,
    the canary reports the site as changed, and somebody spends a morning on a page that did not
    move. It is not on the whitelist, so a file carrying one fails the scan above.
    """
    # AN ESCAPE, NOT THE CHARACTER. This file is committed, so a literal here would be found by
    # the scan above the moment it was tracked: the test would forbid a character and then carry
    # one. It passed while the file was untracked, which is the worst version of the mistake,
    # because git decides when it starts failing.
    smart_quote = "\u2019"
    assert smart_quote not in ALLOWED
    assert unicodedata.name(smart_quote) == "RIGHT SINGLE QUOTATION MARK"
    assert parse_decimal(f"{EURO}19{smart_quote}99") is None, (
        "a smart quote inside a price now parses, which would make this character harmless and "
        "the argument for refusing it weaker than it is"
    )
