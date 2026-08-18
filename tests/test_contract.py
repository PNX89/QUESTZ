from __future__ import annotations

import json

import pytest

from questz.canary import CONTRACT_FORMAT, FieldRule, SelectorRule, load, record, save
from questz.types import ContractError

ROW = 'tr[data-testid="item-row"]'
CONTAINER = '[data-testid="items"]'


def _record(html: str):
    return record(
        html,
        name="items",
        url="http://127.0.0.1:8000/items.html",
        container=CONTAINER,
        ready_when=ROW,
        required=[ROW, f'{ROW} td[data-testid="price"]'],
        fields=[FieldRule("price", f'{ROW} td[data-testid="price"]', "decimal")],
    )


def test_save_and_load_round_trip_is_byte_identical_and_key_sorted(tmp_path, contract):
    first = tmp_path / "items.json"
    save(contract, first)
    second = tmp_path / "again.json"
    save(load(first), second)
    assert first.read_bytes() == second.read_bytes()
    raw = json.loads(first.read_text(encoding="utf-8"))
    assert list(raw) == sorted(raw)
    assert first.read_text(encoding="utf-8").endswith("\n")


def test_record_derives_the_signature_the_baseline_and_pinned_counts(testsite_html):
    recorded = _record(testsite_html("v1/items.html"))
    assert len(recorded.signature) == 64
    assert recorded.baseline[0] == "0|main|data-testid=items"
    assert all(rule.min_count == rule.max_count == 12 for rule in recorded.required)


def test_record_keeps_a_hand_widened_rule_exactly_as_declared(testsite_html):
    recorded = record(
        testsite_html("v1/items.html"),
        name="items",
        url="http://127.0.0.1:8000/items.html",
        container=CONTAINER,
        ready_when=ROW,
        required=[SelectorRule(ROW, min_count=5, max_count=50)],
    )
    assert recorded.required[0].min_count == 5
    assert recorded.required[0].max_count == 50


def test_record_is_stable_across_two_runs(testsite_html):
    html = testsite_html("v1/items.html")
    assert _record(html) == _record(html)


def test_a_container_that_matches_nothing_raises(testsite_html):
    with pytest.raises(ContractError, match="matches nothing"):
        record(
            testsite_html("v2i/items.html"),
            name="items",
            url="http://127.0.0.1:8000/items.html",
            container=CONTAINER,
            ready_when=ROW,
            required=[ROW],
        )


def test_an_unknown_field_shape_raises_naming_the_field(tmp_path, contract):
    path = tmp_path / "items.json"
    save(contract, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fields"][0]["shape"] = "currency"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        load(path)
    assert "'currency'" in str(caught.value)
    assert "'name'" in str(caught.value)


def test_an_enum_field_without_allowed_values_raises(tmp_path, contract):
    path = tmp_path / "items.json"
    save(contract, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fields"][2]["allowed"] = []
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="no 'allowed' values"):
        load(path)


@pytest.mark.parametrize("key", ["container", "required", "signature", "baseline", "format"])
def test_a_missing_key_raises_naming_the_key(tmp_path, contract, key):
    path = tmp_path / "items.json"
    save(contract, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw[key]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError) as caught:
        load(path)
    assert repr(key) in str(caught.value)


def test_a_format_version_mismatch_raises(tmp_path, contract):
    path = tmp_path / "items.json"
    save(contract, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["format"] = CONTRACT_FORMAT + 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ContractError, match="is not supported"):
        load(path)


def test_a_missing_contract_file_raises_a_readable_error(tmp_path):
    with pytest.raises(ContractError, match="contract file not found"):
        load(tmp_path / "absent.json")


def test_unparseable_json_raises_a_readable_error(tmp_path):
    path = tmp_path / "items.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid JSON"):
        load(path)


def test_a_contract_in_the_wrong_encoding_raises_a_readable_error(tmp_path):
    """UnicodeDecodeError is a ValueError, so it is neither an OSError nor a
    JSONDecodeError. Uncaught, it reaches the operator as a traceback."""
    path = tmp_path / "items.json"
    path.write_bytes(json.dumps({"format": 1}).encode("utf-16"))
    with pytest.raises(ContractError, match="not valid UTF-8"):
        load(path)


def test_a_contract_path_that_is_a_directory_raises_a_readable_error(tmp_path):
    (tmp_path / "items.json").mkdir()
    with pytest.raises(ContractError, match="contract file"):
        load(tmp_path / "items.json")
