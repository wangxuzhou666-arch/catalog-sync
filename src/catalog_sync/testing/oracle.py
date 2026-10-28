"""Compare a stored record with what the generated dataset says it must be.

The dataset-as-oracle pattern: the synthetic service is built from a seeded dataset, and a small
``expected_*`` function in the test turns one dataset entry into the fields a correct
pull-normalize run must have stored. The test then asserts ``picked(row, expected) == expected``
for every entry. The expectation is derived from the data, never typed out, so a change in the
generator moves both sides, and a wrong mapping or a wrong parse shows up as a difference.
"""

from __future__ import annotations

from catalog_sync.models import JsonObject


def sorted_issues(row: JsonObject) -> list[str]:
    """A record's ``issues`` in a stable order, since the order they were found in is not a fact."""
    issues = row["issues"]
    assert isinstance(issues, list)
    return sorted(str(issue) for issue in issues)


def picked(row: JsonObject, expected: JsonObject) -> JsonObject:
    """``row`` reduced to the keys ``expected`` talks about, so the two can be compared whole.

    ``issues`` are sorted; ``images`` are reduced to the keys each expected picture lists, in
    order, which leaves out the ones a test cannot predict (a checksum, a byte count).
    """
    narrowed: JsonObject = {}
    for key, wanted in expected.items():
        value = row[key]
        if key == "issues":
            value = sorted_issues(row)
        if key == "images":
            assert isinstance(value, list) and isinstance(wanted, list)
            value = [
                {name: image[name] for name in wanted[position]}
                for position, image in enumerate(value)
            ]
        narrowed[key] = value
    return narrowed
