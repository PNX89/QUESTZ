"""The bundled target.

These tests talk to 127.0.0.1 and nothing else. Loopback traffic to a server this process
started is not network access.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest
from pricewatch import DEMO_PASSWORD, DEMO_USER
from scenarios import TABLE_ROWS

from questz.canary import Contract
from questz.normalize import matches, parse, select, walk
from questz.testsite import TESTSITE_ROOT, VARIANTS, serve, variant_dir
from questz.types import QuestzError

REFERENCED = (
    *(relative for relative, _ in TABLE_ROWS),
    "v1/index.html",
    "v1/login.html",
    "v1/app.js",
    "v2/index.html",
    "v2/login.html",
    "v2/app.js",
    "v2i/items.html",
)


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def test_the_server_binds_loopback_on_an_assigned_port_and_serves_the_items_page(testsite_server):
    base_url = testsite_server("v1")
    assert base_url.startswith("http://127.0.0.1:")
    assert int(base_url.rsplit(":", 1)[1]) > 0
    status, body = _get(f"{base_url}/items.html")
    assert status == 200
    assert 'data-testid="items-table"' in body


def test_an_unknown_path_is_a_404(testsite_server):
    base_url = testsite_server("v1")
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(f"{base_url}/no-such-page.html")
    assert caught.value.code == 404


@pytest.mark.parametrize("relative", REFERENCED)
def test_every_file_the_contract_and_the_scenarios_reference_exists(relative: str):
    assert (TESTSITE_ROOT / relative).is_file()


def test_the_contract_anchors_resolve_in_the_recorded_page(contract: Contract):
    html = (TESTSITE_ROOT / "v1" / "items.html").read_text(encoding="utf-8")
    assert len(matches(html, contract.container)) == 1
    assert len(matches(html, contract.ready_when)) == 12
    for selector in contract.secret_selectors:
        assert matches((TESTSITE_ROOT / "v1" / "login.html").read_text(encoding="utf-8"), selector)


@pytest.mark.parametrize("variant", ["v1", "v2"])
def test_every_element_showing_a_credential_is_a_secret_selector(contract: Contract, variant: str):
    """The mask list has to cover what renders a credential, not only what accepts one. A
    hint line, a prefilled value or an error echoing input is how masking fails on a real
    portal, and a committed screenshot is only evidence if the list is complete."""
    root = parse((TESTSITE_ROOT / variant / "login.html").read_text(encoding="utf-8"))
    masked = {
        id(element) for selector in contract.secret_selectors for element in select(root, selector)
    }
    showing = [
        node
        for node in walk(root)
        if any(DEMO_PASSWORD in part or DEMO_USER in part for part in node.text_parts)
    ]
    assert showing, "this fixture is supposed to print the demo credentials somewhere"
    assert [node.tag for node in showing if id(node) not in masked] == []


def test_two_servers_on_port_zero_do_not_collide():
    """A fixed port is the classic CI "address already in use" flake."""
    first, first_url = serve("v1")
    second, second_url = serve("v1")
    try:
        assert first_url != second_url
    finally:
        first.server_close()
        second.server_close()


def test_an_unknown_variant_names_the_ones_that_exist():
    with pytest.raises(QuestzError) as caught:
        variant_dir("v3")
    assert "v3" in str(caught.value)
    assert all(variant in str(caught.value) for variant in VARIANTS)
