from __future__ import annotations

import pytest

from catalog_sync.envelope import peel_data, require_state_ok
from catalog_sync.errors import SourceShapeError
from tests.fixtures import sample_items


def test_depth_two_reaches_the_record_and_depth_one_does_not() -> None:
    envelope = sample_items.detail_envelope(3)
    record = sample_items.detail_record(3)
    assert peel_data(envelope, 2) == record
    assert peel_data(envelope, 1) == {"data": record}, "one layer leaves the inner wrapper behind"


def test_a_record_with_its_own_data_field_is_not_over_peeled() -> None:
    record = {"code": "DEMO-0001", "data": {"nested": "owned by the record"}}
    envelope = {"state": True, "data": {"data": record}}
    assert peel_data(envelope, 2) == record


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": []},
        {"data": "text"},
        {"data": {"records": 1}},  # one layer present, second missing
        {"data": {"data": [1, 2]}},  # second layer is a list, not an object
    ],
)
def test_wrong_shape_raises(payload: dict[str, object]) -> None:
    with pytest.raises(SourceShapeError):
        peel_data(payload, 2)


def test_depth_must_be_positive() -> None:
    with pytest.raises(ValueError, match="depth"):
        peel_data({"data": {}}, 0)


def test_state_true_passes() -> None:
    require_state_ok({"state": True, "data": {}})


def test_state_false_raises_and_carries_the_server_message() -> None:
    with pytest.raises(SourceShapeError, match="state is false: token rejected"):
        require_state_ok({"state": False, "msg": "token rejected"})


@pytest.mark.parametrize("state", [None, 1, "true", "ok", [], {}])
def test_state_must_be_a_real_boolean(state: object) -> None:
    with pytest.raises(SourceShapeError, match="boolean 'state'"):
        require_state_ok({"state": state})


def test_missing_state_raises() -> None:
    with pytest.raises(SourceShapeError):
        require_state_ok({"data": {}})
