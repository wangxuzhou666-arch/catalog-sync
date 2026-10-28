"""The acceptance scenarios. Each is a function ``(harness, record) -> None`` registered by id.

``record.check(name, passed, detail)`` states what must hold, with the observation behind it;
``record.evidence(key, value)`` keeps a number or a list for the report; ``raise NotRun(reason)``
says the scenario cannot run here. W4a wrote S2, S3, S8 and S9; the scenarios W4b owns are listed
as placeholders at the bottom and show up as NOT_RUN until they are written.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from acceptance_compare import (
    Key,
    Oracle,
    Record,
    compare_stores,
    find_values,
    oracle_mismatches,
    owned_fields,
    postgres_schema,
    read_jsonl,
    read_postgres,
    read_sidecar,
    read_sqlite,
    sample_ids,
    tree_blobs,
)
from acceptance_lab import (
    ENTITIES,
    PASSWORD_ENV,
    PLATFORM_TOKEN_ENV,
    SINK_KINDS,
    Entity,
    Harness,
    Lab,
    Request,
    Run,
    RunningCommand,
    SourceProcess,
    count_kinds,
    per_item,
    real_workspace_locations,
    scenario,
    sink_name,
)
from acceptance_report import NotRun, Recorder
from catalog_sync.testing.workspace_guard import changes, fingerprint

EXIT_AUTH = 2
EXIT_COMPLIANCE = 4
TOKEN_TTL_S = 2.0  # short enough not to slow the run; the scenario then waits longer than this
# A login that runs out in the middle of a pull is made by count, not by clock: the source answers
# 401 to the data request after the Nth since the login (`mock serve --expire-after`), so no pause
# between requests has to be tuned to a token's life.
EXPIRE_AFTER_REQUESTS = 2
_LOAD_COUNTS = re.compile(
    r"load -> (?P<sink>\S+) \(\w+\), written:\n\s+inserted (?P<inserted>\d+), "
    r"updated (?P<updated>\d+), unchanged (?P<unchanged>\d+)"
)
PENDING_W4B = "not implemented yet (W4b)"
HTTP_NOT_FOUND = 404
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
MATERIALS, SUPPLIERS = ENTITIES
_RUN_ID = re.compile(r"^(?P<id>\d{8}T\d{6}Z-[0-9a-f]{6})\s", re.MULTILINE)
# "detail : 16 pending, 8 done" or "detail : 24 done"
_STAGE_DONE = re.compile(
    r"^\s*(?P<stage>list|detail|assets)\s*:[^\n]*?(?P<done>\d+) done", re.MULTILINE
)


def load_counts(stdout: str) -> dict[str, tuple[int, int, int]]:
    """The (inserted, updated, unchanged) each sink reported in a command's output."""
    return {
        found["sink"]: (int(found["inserted"]), int(found["updated"]), int(found["unchanged"]))
        for found in _LOAD_COUNTS.finditer(stdout)
    }


def sign_in(lab: Lab, record: Recorder, *, force: bool = False) -> None:
    login = lab.cli("login", "mock", "--form", *(["--force"] if force else []))
    record.check("login --form exits 0", login.returncode == 0, login.stderr.strip()[:200])


def run_ids(lab: Lab) -> list[str]:
    """The ids of the runs the workspace knows, newest first (`runs list`)."""
    return [found["id"] for found in _RUN_ID.finditer(lab.cli("runs", "list").stdout)]


def stage_counts(shown: str) -> dict[str, int]:
    """The "detail : N done" lines of `runs show`."""
    return {found["stage"]: int(found["done"]) for found in _STAGE_DONE.finditer(shown)}


def normalize_and_load(lab: Lab, entity: Entity, record: Recorder) -> Path:
    """Normalize the latest run of the entity and load it into every sink the config names for it.

    Returns the normalized file, which `_check_fields` then compares with every store."""
    normalized = lab.work / f"{entity.name}.normalized.jsonl"
    done = lab.cli("normalize", entity.source, "--out", normalized)
    record.check(f"{entity.name}: normalize exits 0", done.returncode == 0, done.stderr[:200])
    for kind in SINK_KINDS:
        loaded = lab.cli("load", "--sink", sink_name(kind, entity), "--input", normalized)
        record.check(
            f"{entity.name}: load --sink {sink_name(kind, entity)} exits 0",
            loaded.returncode == 0,
            loaded.stderr[:200],
        )
    return normalized


# --- S1: resume ---------------------------------------------------------------------------------

KILL_GAP_S = 0.1  # the pause between requests while a pull is to be killed: slow enough to catch
KILL_AFTER_DETAILS = 9  # of 24 details; the first kill comes in the detail stage
KILL_AFTER_ASSETS = 15  # of 48 pictures; the second kill comes in the asset stage


@scenario("S1", "Resume after a killed pull: finished work is not fetched again", order=1)
def resume(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    ids = oracle.ids(MATERIALS)
    with harness.lab("S1") as lab:
        source = lab.start_source()
        lab.start_platform()
        lab.write_config(gap_s=KILL_GAP_S)
        sign_in(lab, record)
        phases = [_pull_and_kill(lab, source, record, "detail", KILL_AFTER_DETAILS)]
        run_id = _the_only_run(lab, record)
        _check_interrupted(lab, record, run_id, len(ids))
        phases.append(_resume_and_kill(lab, source, record, run_id, "asset", KILL_AFTER_ASSETS))
        phases.append(_resume_to_the_end(lab, source, record, run_id))
        _check_not_fetched_twice(record, ids, phases)
        _check_complete(lab, record, run_id, len(ids))
        normalize_and_load(lab, MATERIALS, record)
        _check_fields(lab, oracle, MATERIALS, record)
        _check_row_counts(record, MATERIALS, len(ids), [_row_counts(lab, MATERIALS)])


def _pull_and_kill(
    lab: Lab, source: SourceProcess, record: Recorder, kind: str, after: int
) -> list[Request]:
    """A pull, SIGKILLed the moment the source has seen `after` requests of `kind` from it."""
    mark = source.mark()
    pull = lab.start_command("pull", MATERIALS.source)
    return _kill_at(source, record, pull, mark, kind, after)


def _kill_at(
    source: SourceProcess,
    record: Recorder,
    command: RunningCommand,
    mark: int,
    kind: str,
    after: int,
) -> list[Request]:
    source.wait_for(kind, after, since=mark, is_alive=lambda: command.is_running)
    command.kill()
    ended = command.finish()
    source.settle()  # a request the dying client had sent may still be logged
    made = source.since(mark)
    record.check(
        f"the pull was killed (SIGKILL) after {after} {kind} request(s) it was seen to make",
        ended.returncode == -signal.SIGKILL,
        f"exit {ended.returncode}, {count_kinds(made)}",
    )
    return made


def _the_only_run(lab: Lab, record: Recorder) -> str:
    runs = run_ids(lab)
    record.check("the workspace holds exactly one run", len(runs) == 1, f"{len(runs)} run(s)")
    return runs[0]


def _check_interrupted(lab: Lab, record: Recorder, run_id: str, total: int) -> None:
    listed = lab.cli("runs", "list").stdout
    shown = lab.cli("runs", "show", run_id).stdout
    done = stage_counts(shown)
    record.evidence("after the first kill: stages done", done)
    record.check(
        "the killed run is left unfinished (status running), mid-way through the detail stage",
        " running " in listed and 0 < done.get("detail", 0) < total and done.get("assets", 0) == 0,
        str(done),
    )


def _resume_and_kill(
    lab: Lab, source: SourceProcess, record: Recorder, run_id: str, kind: str, after: int
) -> list[Request]:
    mark = source.mark()
    pull = lab.start_command("pull", MATERIALS.source, "--resume", run_id)
    made = _kill_at(source, record, pull, mark, kind, after)
    shown = lab.cli("runs", "show", run_id).stdout
    record.evidence("after the second kill: stages done", stage_counts(shown))
    return made


def _resume_to_the_end(
    lab: Lab, source: SourceProcess, record: Recorder, run_id: str
) -> list[Request]:
    mark = source.mark()
    resumed = lab.cli("pull", MATERIALS.source, "--resume", run_id)
    record.check(
        "the last `pull --resume` exits 0", resumed.returncode == 0, resumed.stderr.strip()[:200]
    )
    return source.since(mark)


def _check_not_fetched_twice(
    record: Recorder, ids: Sequence[str], phases: Sequence[Sequence[Request]]
) -> None:
    """Across the first kill, the second kill and the end: every detail and every picture once,
    except the one request that was in flight when a kill came (it had no answer to store yet)."""
    in_flight = {phase[-1].path for phase in phases[:-1]}
    for kind, wanted in (("detail", 1), ("asset", 2)):
        paths = Counter(r.path for phase in phases for r in phase if r.kind == kind)
        repeated = {path for path, count in paths.items() if count > 1}
        record.evidence(
            f"{kind} requests per phase (kill, kill, end)",
            [count_kinds(phase).get(kind, 0) for phase in phases],
        )
        record.check(
            f"no {kind} request was made twice, except one that was in flight at a kill",
            repeated <= in_flight and max(paths.values()) <= 2,
            f"{len(repeated)} repeated of {len(paths)} distinct",
        )
        items: Counter[str] = Counter()
        for phase in phases:
            items.update(per_item(phase, kind))
        record.check(
            f"every one of the {len(ids)} items got its {wanted} {kind} request(s), none missed",
            set(items) == set(ids) and all(count >= wanted for count in items.values()),
            f"{len(items)} items",
        )
    listing = [count_kinds(phase).get("list", 0) for phase in phases]
    record.check(
        "the resumed pulls did not walk the listing again (it was finished before the first kill)",
        listing[1:] == [0, 0],
        f"listing requests per phase: {listing}",
    )
    record.check(
        "the first kill fell in the detail stage (no picture was fetched yet), the second in the "
        "asset stage (every detail was done)",
        count_kinds(phases[0]).get("asset", 0) == 0
        and {r.item for p in phases[:2] for r in p if r.kind == "detail"} == set(ids),
    )


def _check_complete(lab: Lab, record: Recorder, run_id: str, total: int) -> None:
    shown = lab.cli("runs", "show", run_id).stdout
    done = stage_counts(shown)
    record.evidence("the final run: stages done", done)
    record.check(
        f"the resumed run is complete: status complete, {total} items, every stage done",
        "status:   complete" in shown
        and f"items:    {total}" in shown
        and done == {"list": total, "detail": total, "assets": total}
        and "listing:  complete" in shown,
        str(done),
    )


# --- S2: re-run ---------------------------------------------------------------------------------


@scenario("S2", "Re-run: no refetch, no duplicate rows, replayed keys, equal fields", order=2)
def rerun(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    with harness.lab("S2") as lab:
        lab.start_source()
        lab.start_platform()
        lab.write_config()
        sign_in(lab, record)
        for entity in ENTITIES:
            _rerun_entity(lab, oracle, entity, record)


def _rerun_entity(lab: Lab, oracle: Oracle, entity: Entity, record: Recorder) -> None:
    assert lab.source is not None and lab.platform is not None
    expected_ids = oracle.ids(entity)
    runs = []
    for number in (1, 2):
        mark = lab.source.mark()
        command = lab.cli("run", entity.source)
        requests = lab.source.since(mark)
        record.evidence(
            f"{entity.name}: idempotency keys the platform holds after run {number}",
            len(lab.platform.idempotency_keys()),
        )
        record.check(
            f"{entity.name}: run {number} exits 0", command.returncode == 0, command.stderr[:200]
        )
        runs.append((command, requests, _row_counts(lab, entity)))
    _check_requests(record, entity, expected_ids, runs[0][1], runs[1][1])
    _check_row_counts(record, entity, len(expected_ids), [counts for _, _, counts in runs])
    _check_second_load(record, entity, runs[1][0].stdout)
    _check_replay(lab, record, entity)
    _check_fields(lab, oracle, entity, record)


def _row_counts(lab: Lab, entity: Entity) -> dict[str, int]:
    assert lab.platform is not None
    return {
        "sqlite": len(read_sqlite(lab.sqlite_path, entity.table)),
        "platform": len(read_sqlite(lab.platform.db, entity.schema)),
        "jsonl sink": len(read_jsonl(lab.jsonl_path(entity))),
    }


def _check_requests(
    record: Recorder,
    entity: Entity,
    ids: Sequence[str],
    first: Sequence[Request],
    second: Sequence[Request],
) -> None:
    """Run 1 must show the counter sees fetches (else "zero" would prove nothing); run 2 none."""
    first_kinds, second_kinds = count_kinds(first), count_kinds(second)
    record.evidence(f"{entity.name}: requests in run 1", first_kinds)
    record.evidence(f"{entity.name}: requests in run 2", second_kinds)
    has_details = entity.name == "materials"
    record.check(
        f"{entity.name}: run 1 fetched each item's detail once and each picture once",
        (
            per_item(first, "detail") == dict.fromkeys(ids, 1)
            and per_item(first, "asset") == dict.fromkeys(ids, 2)
        )
        if has_details
        else (first_kinds.get("detail", 0) == 0 and first_kinds.get("asset", 0) == 0),
        f"detail {first_kinds.get('detail', 0)}, asset {first_kinds.get('asset', 0)}",
    )
    record.check(
        f"{entity.name}: run 2 made zero detail and zero asset requests",
        second_kinds.get("detail", 0) == 0 and second_kinds.get("asset", 0) == 0,
        f"detail {second_kinds.get('detail', 0)}, asset {second_kinds.get('asset', 0)}",
    )
    record.check(
        f"{entity.name}: run 2 still walked the listing, as many pages as run 1",
        second_kinds.get("list", 0) == first_kinds.get("list", 0) > 0,
        f"{second_kinds.get('list', 0)} vs {first_kinds.get('list', 0)} listing requests",
    )


def _check_row_counts(
    record: Recorder, entity: Entity, expected: int, after_each: Sequence[dict[str, int]]
) -> None:
    record.evidence(f"{entity.name}: expected row count (the seeded catalogue)", expected)
    record.evidence(f"{entity.name}: rows after run 1, after run 2", list(after_each))
    for number, counts in enumerate(after_each, start=1):
        record.check(
            f"{entity.name}: after run {number}, SQLite, the platform and the JSONL sink hold "
            f"the {expected} seeded rows",
            set(counts.values()) == {expected},
            str(counts),
        )


def _check_second_load(record: Recorder, entity: Entity, stdout: str) -> None:
    counts = load_counts(stdout)
    wanted = {sink_name(kind, entity) for kind in ("sqlite", "platform")}
    record.evidence(f"{entity.name}: run 2 load counts (inserted, updated, unchanged)", counts)
    record.check(
        f"{entity.name}: run 2 inserted and updated nothing in SQLite or on the platform",
        wanted <= counts.keys() and all(counts[name][:2] == (0, 0) for name in wanted),
        str({name: counts.get(name) for name in sorted(wanted)}),
    )


def _check_replay(lab: Lab, record: Recorder, entity: Entity) -> None:
    """The same normalized file loaded to the platform again is answered from the stored result."""
    assert lab.platform is not None
    platform = lab.platform
    normalized = lab.work / f"{entity.name}.normalized.jsonl"
    done = lab.cli("normalize", entity.source, "--out", normalized)
    record.check(f"{entity.name}: normalize of the latest run exits 0", done.returncode == 0)
    sink = sink_name("platform", entity)
    rows_before = len(read_sqlite(platform.db, entity.schema))
    marks = []
    for number in (1, 2):
        before = len(platform.events())
        loaded = lab.cli("load", "--sink", sink, "--input", normalized)
        record.check(
            f"{entity.name}: load --sink {sink} (time {number}) exits 0", loaded.returncode == 0
        )
        marks.append((platform.events()[before:], list(platform.idempotency_keys())))
    (first_events, first_keys), (second_events, second_keys) = marks
    record.evidence(
        f"{entity.name}: platform answers to the same file, loaded twice",
        {
            "first": [(e.kind, e.key_prefix) for e in first_events],
            "second": [(e.kind, e.key_prefix) for e in second_events],
            "stored keys after each": [len(first_keys), len(second_keys)],
        },
    )
    record.check(
        f"{entity.name}: loading the same file again is a replay under the same idempotency key",
        bool(second_events)
        and all(event.kind == "replayed" for event in second_events)
        and [e.key_prefix for e in second_events] == [e.key_prefix for e in first_events]
        and first_keys == second_keys,
        f"{[e.kind for e in second_events]}",
    )
    rows_after = len(read_sqlite(platform.db, entity.schema))
    record.check(
        f"{entity.name}: the replays added no row on the platform",
        rows_after == rows_before,
        f"{rows_before} rows before, {rows_after} after",
    )


def _check_fields(
    lab: Lab,
    oracle: Oracle,
    entity: Entity,
    record: Recorder,
    *,
    expected_ids: Sequence[str] | None = None,
) -> None:
    """Owned fields agree across the normalized file and every sink; a sample matches the oracle.

    `expected_ids` are the ids a run was meant to load (all of the catalogue unless some failed)."""
    assert lab.source is not None and lab.platform is not None
    normalized = lab.work / f"{entity.name}.normalized.jsonl"
    fields = owned_fields(read_sidecar(normalized))
    stores = {
        "normalized JSONL": read_jsonl(normalized),
        "JSONL sink": read_jsonl(lab.jsonl_path(entity)),
        "SQLite": read_sqlite(lab.sqlite_path, entity.table),
        "platform": read_sqlite(lab.platform.db, entity.schema),
    }
    comparison = compare_stores(stores, "normalized JSONL", fields)
    record.evidence(
        f"{entity.name}: field comparison",
        {**comparison.evidence(), "owned fields compared": fields},
    )
    record.check(
        f"{entity.name}: every owned field agrees across the normalized JSONL, the JSONL sink, "
        "SQLite and the platform",
        comparison.is_equal,
        f"{comparison.records} records, {comparison.fields} field values compared, "
        f"{len(comparison.mismatches)} mismatch(es)",
    )
    sample = sample_ids(expected_ids or oracle.ids(entity))
    wrong = oracle_mismatches(oracle, entity, lab.source.base_url, stores, sample)
    record.evidence(f"{entity.name}: oracle sample", {"ids": sample, "mismatches": wrong[:20]})
    record.check(
        f"{entity.name}: a fixed sample of {len(sample)} ids matches the oracle in all four stores",
        not wrong,
        f"{len(wrong)} mismatch(es)",
    )


# --- S3: offline re-normalize -------------------------------------------------------------------

# One field rule changed per entity, and what that must do to every record.
_MAPPING_CHANGES = {
    "materials": ("currency: USD", "currency: EUR"),  # every price is now stated in EUR
    "suppliers": ("from: [list.country]", "from: [list.name]"),  # country now holds the name
}


@scenario("S3", "Offline re-normalize with a changed mapping", order=3)
def offline_renormalize(harness: Harness, record: Recorder) -> None:
    with harness.lab("S3") as lab:
        source = lab.start_source()
        lab.write_config(sinks=("sqlite",))
        sign_in(lab, record)
        baselines = {entity.name: _pull_and_normalize(lab, entity, record) for entity in ENTITIES}
        source.stop()
        record.check("the Mock source is stopped and its port is closed", not source.is_listening())
        for entity in ENTITIES:
            _renormalize_offline(harness, lab, entity, baselines[entity.name], record)


def _pull_and_normalize(lab: Lab, entity: Entity, record: Recorder) -> Path:
    pulled = lab.cli("pull", entity.source)
    record.check(f"{entity.name}: pull exits 0", pulled.returncode == 0, pulled.stderr[:200])
    baseline = lab.work / f"{entity.name}.baseline.jsonl"
    done = lab.cli("normalize", entity.source, "--out", baseline)
    record.check(
        f"{entity.name}: normalize with the packaged mapping exits 0", done.returncode == 0
    )
    return baseline


def _renormalize_offline(
    harness: Harness, lab: Lab, entity: Entity, baseline: Path, record: Recorder
) -> None:
    old, new = _MAPPING_CHANGES[entity.name]
    packaged = (
        harness.installed.site_packages
        / "catalog_sync/plugins/mock/mappings"
        / f"{entity.name}.yaml"
    )
    text = packaged.read_text(encoding="utf-8")
    record.check(
        f"{entity.name}: the change touches exactly one rule of the packaged mapping",
        text.count(old) == 1,
        f"{old!r} -> {new!r}",
    )
    changed_mapping = lab.work / f"{entity.name}.changed.yaml"
    changed_mapping.write_text(text.replace(old, new), encoding="utf-8")
    changed = lab.work / f"{entity.name}.changed.jsonl"
    done = lab.cli("normalize", entity.source, "--mapping", changed_mapping, "--out", changed)
    record.check(
        f"{entity.name}: normalize --mapping exits 0 with the source stopped",
        done.returncode == 0,
        done.stderr[:200],
    )
    before, after = read_jsonl(baseline), read_jsonl(changed)
    predicted = {key: _apply_change(entity, row) for key, row in before.items()}
    differing = [key[1] for key in before if _stable(before[key]) != _stable(after.get(key, {}))]
    record.evidence(f"{entity.name}: records that the change altered", len(differing))
    record.check(
        f"{entity.name}: the output differs from the baseline exactly as the change predicts",
        bool(differing)
        and {k: _stable(v) for k, v in predicted.items()}
        == {k: _stable(v) for k, v in after.items()},
        f"{len(differing)} of {len(before)} records changed",
    )
    old_meta, new_meta = read_sidecar(baseline), read_sidecar(changed)
    record.check(
        f"{entity.name}: the sidecar records the new mapping on the same run",
        old_meta["mapping_sha256"] != new_meta["mapping_sha256"]
        and old_meta["run_id"] == new_meta["run_id"]
        and old_meta["record_count"] == new_meta["record_count"],
    )


def _stable(row: Record) -> Record:
    """A record without `normalized_at`, which changes on every normalize."""
    return {key: value for key, value in row.items() if key != "normalized_at"}


def _apply_change(entity: Entity, row: Record) -> Record:
    changed = dict(row)
    if entity.name == "materials":
        price = row["price"]
        changed["price"] = {**price, "currency": "EUR"} if isinstance(price, dict) else price
    else:
        changed["country"] = row["name"]
    return changed


# --- S4: partial upsert -------------------------------------------------------------------------

EXTRA_COLUMN = (
    "ops_note"  # a column the target owns: the tool never wrote it, so it never touches it
)


@scenario("S4", "Partial upsert keeps a platform-owned column and a child table", order=4)
def partial_upsert(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    with harness.lab("S4") as lab:
        lab.start_source()
        lab.start_platform()
        lab.write_config()
        sign_in(lab, record)
        for entity in ENTITIES:
            _upsert_over_extras(lab, oracle, entity, record)


def _upsert_over_extras(lab: Lab, oracle: Oracle, entity: Entity, record: Recorder) -> None:
    assert lab.platform is not None
    first = lab.cli("run", entity.source)
    record.check(f"{entity.name}: the first run exits 0", first.returncode == 0, first.stderr[:200])
    children = _add_extras(lab.sqlite_path, entity)
    _add_extras_to_platform(lab.platform.db, entity.schema)
    sqlite_before = read_sqlite(lab.sqlite_path, entity.table)
    platform_before = read_sqlite(lab.platform.db, entity.schema)
    # `pull --full` refetches everything into a new run, so every row differs in raw_ref and
    # fetched_at: the load below really writes over the rows that carry the extras.
    pulled = lab.cli("pull", entity.source, "--full")
    record.check(f"{entity.name}: pull --full exits 0", pulled.returncode == 0, pulled.stderr[:200])
    normalized = lab.work / f"{entity.name}.normalized.jsonl"
    record.check(
        f"{entity.name}: normalize exits 0",
        lab.cli("normalize", entity.source, "--out", normalized).returncode == 0,
    )
    for kind in ("sqlite", "platform"):
        loaded = lab.cli("load", "--sink", sink_name(kind, entity), "--input", normalized)
        counts = load_counts(loaded.stdout).get(sink_name(kind, entity))
        record.evidence(f"{entity.name}: {kind} load (inserted, updated, unchanged)", counts)
        record.check(
            f"{entity.name}: the {kind} load rewrote the owned fields of every row (all updated)",
            loaded.returncode == 0 and counts == (0, len(sqlite_before), 0),
            f"exit {loaded.returncode}, {counts}",
        )
    sqlite_after = read_sqlite(lab.sqlite_path, entity.table)
    platform_after = read_sqlite(lab.platform.db, entity.schema)
    _check_extras_kept(record, entity, "SQLite", sqlite_before, sqlite_after)
    _check_extras_kept(record, entity, "platform", platform_before, platform_after)
    _check_child_rows(record, entity, lab.sqlite_path, children)
    fields = owned_fields(read_sidecar(normalized))
    comparison = compare_stores(
        {
            "normalized JSONL": read_jsonl(normalized),
            "SQLite": sqlite_after,
            "platform": platform_after,
        },
        "normalized JSONL",
        fields,
    )
    record.check(
        f"{entity.name}: the owned fields now hold the new load's values in SQLite and on the "
        "platform, next to the extras",
        comparison.is_equal,
        f"{comparison.fields} field values compared, {len(comparison.mismatches)} mismatch(es)",
    )


def child_table(entity: Entity) -> str:
    return f"ops_events_{entity.name}"


def _add_extras(path: Path, entity: Entity) -> list[tuple[object, ...]]:
    """An extra column with a value per row, and a child table with two foreign-key rows per row."""
    table, child = entity.table, child_table(entity)
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {EXTRA_COLUMN} TEXT')
        conn.execute(f"UPDATE \"{table}\" SET {EXTRA_COLUMN} = 'note-' || source_id")  # noqa: S608 — constants of this file
        conn.execute(
            f"CREATE TABLE {child} (id INTEGER PRIMARY KEY, source TEXT NOT NULL, "
            "source_id TEXT NOT NULL, note TEXT NOT NULL, "
            f'FOREIGN KEY (source, source_id) REFERENCES "{table}" (source, source_id) '
            "ON DELETE CASCADE)"
        )
        conn.execute(
            f"INSERT INTO {child} (source, source_id, note) "  # noqa: S608 — constants of this file
            f"SELECT source, source_id, 'first ' || source_id FROM \"{table}\" "
            "UNION ALL "
            f"SELECT source, source_id, 'second ' || source_id FROM \"{table}\""
        )
        return list(conn.execute(f"SELECT * FROM {child} ORDER BY id"))  # noqa: S608


def _add_extras_to_platform(path: Path, table: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {EXTRA_COLUMN} TEXT')
        conn.execute(
            f"UPDATE \"{table}\" SET {EXTRA_COLUMN} = 'platform-note-' || source_id"  # noqa: S608
        )


def _check_extras_kept(
    record: Recorder,
    entity: Entity,
    where: str,
    before: dict[Key, Record],
    after: dict[Key, Record],
) -> None:
    kept = {key: row.get(EXTRA_COLUMN) for key, row in after.items()}
    record.check(
        f"{entity.name}: the {where} column {EXTRA_COLUMN}, added by the target, holds the "
        "values it had before the load",
        bool(before)
        and kept == {key: row.get(EXTRA_COLUMN) for key, row in before.items()}
        and all(value is not None for value in kept.values()),
        f"{len(kept)} rows",
    )
    record.check(
        f"{entity.name}: the load added no column of its own to the {where} table beyond its owned "
        "fields",
        set(after[next(iter(after))]) - set(before[next(iter(before))]) == set(),
    )


def _check_child_rows(
    record: Recorder, entity: Entity, path: Path, before: list[tuple[object, ...]]
) -> None:
    child = child_table(entity)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        after = list(conn.execute(f"SELECT * FROM {child} ORDER BY id"))  # noqa: S608
        broken = list(conn.execute("PRAGMA foreign_key_check"))
    record.check(
        f"{entity.name}: the child table's {len(before)} foreign-key rows all survive, unchanged",
        bool(before) and after == before and not broken,
        f"{len(after)} rows after, {len(broken)} broken reference(s)",
    )


# --- S5: partial failure ------------------------------------------------------------------------

GONE_EVERY = 12  # mock serve --gone-every: the details of mat-0012 and mat-0024 answer 404
_FAILED_LINE = re.compile(r"^\s*failed (?P<stage>\w+) (?P<id>\S+): (?P<reason>.*)$", re.MULTILINE)
_FAILED_SHARE = re.compile(
    r"(?P<failed>\d+) of (?P<tried>\d+) fetched item\(s\) failed \((?P<share>[\d.]+)%\), "
    r"(?P<verdict>within|over) the limit of (?P<limit>[\d.]+)%"
)
EXIT_WITH_FAILURES = 5


@scenario("S5", "Partial failure: exit 5, failed items listed, the rest loaded", order=5)
def partial_failure(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    all_ids = oracle.ids(MATERIALS)
    failed = all_ids[GONE_EVERY - 1 :: GONE_EVERY]  # the Nth, 2Nth, ... in catalogue order
    record.evidence("the items whose detail the source refuses (--gone-every)", failed)
    with harness.lab("S5") as lab:
        source = lab.start_source(gone_every=GONE_EVERY)
        lab.start_platform()
        lab.write_config()
        sign_in(lab, record)
        done = lab.cli("run", MATERIALS.source)
        _check_failed_run(lab, record, done, failed, len(all_ids), source.since(0))
        _check_loaded_without_the_failed(lab, oracle, record, failed)
    with harness.lab("S5-strict") as strict:
        strict.start_source(gone_every=GONE_EVERY)
        strict.start_platform()
        strict.write_config()
        sign_in(strict, record)
        over = strict.cli("run", MATERIALS.source, "--max-failed-ratio", "0")
        share = _FAILED_SHARE.search(over.stderr)
        record.check(
            "the same run with --max-failed-ratio 0 exits 1: the same failed items are over "
            "the limit",
            over.returncode == 1 and share is not None and share["verdict"] == "over",
            f"exit {over.returncode}: {share[0] if share else over.stderr.strip()[-200:]}",
        )
        loaded = len(read_sqlite(strict.sqlite_path, MATERIALS.table))
        record.check(
            "a run that counts as failed still loaded what arrived",
            loaded == len(all_ids) - len(failed),
            f"{loaded} rows in SQLite",
        )


def _check_failed_run(
    lab: Lab,
    record: Recorder,
    done: Run,
    failed: Sequence[str],
    total: int,
    made: Sequence[Request],
) -> None:
    share = _FAILED_SHARE.search(done.stderr)
    record.check(
        f"the run exits {EXIT_WITH_FAILURES}: it finished, with failed items",
        done.returncode == EXIT_WITH_FAILURES,
        f"exit {done.returncode}",
    )
    record.check(
        "the failed share is within the limit (max_failed_ratio, default) and says so",
        share is not None
        and share["verdict"] == "within"
        and int(share["failed"]) == len(failed)
        and int(share["tried"]) == total
        and float(share["share"]) <= float(share["limit"]),
        share[0] if share else done.stderr.strip()[-200:],
    )
    run_id = run_ids(lab)[0]
    listed = {m["id"]: m for m in _FAILED_LINE.finditer(lab.cli("runs", "show", run_id).stdout)}
    record.check(
        "`runs show` lists exactly the failed items, each with its stage and its reason",
        set(listed) == set(failed)
        and all(m["stage"] == "detail" and "404" in m["reason"] for m in listed.values()),
        str({i: m["reason"] for i, m in listed.items()}),
    )
    refused = per_item([r for r in made if r.status == HTTP_NOT_FOUND], "detail")
    record.check(
        "the source refused each failed item's detail once, and no picture of it was fetched",
        refused == dict.fromkeys(failed, 1) and not set(failed) & set(per_item(made, "asset")),
        f"{refused}",
    )


def _check_loaded_without_the_failed(
    lab: Lab, oracle: Oracle, record: Recorder, failed: Sequence[str]
) -> None:
    assert lab.platform is not None
    wanted = [i for i in oracle.ids(MATERIALS) if i not in failed]
    normalized = lab.work / "materials.normalized.jsonl"
    done = lab.cli("normalize", MATERIALS.source, "--out", normalized)
    record.check("normalize of the failed run exits 0", done.returncode == 0)
    stores = {
        "normalized JSONL": read_jsonl(normalized),
        "JSONL sink": read_jsonl(lab.jsonl_path(MATERIALS)),
        "SQLite": read_sqlite(lab.sqlite_path, MATERIALS.table),
        "platform": read_sqlite(lab.platform.db, MATERIALS.schema),
    }
    record.evidence("rows loaded, per store", {name: len(rows) for name, rows in stores.items()})
    record.check(
        f"every store holds exactly the {len(wanted)} items that did not fail "
        f"({len(wanted) + len(failed)} - {len(failed)}), and none of the failed ones",
        all(
            sorted(source_id for _, source_id in rows) == sorted(wanted) for rows in stores.values()
        ),
    )
    _check_fields(lab, oracle, MATERIALS, record, expected_ids=wanted)


# --- S6: a vanished source record ---------------------------------------------------------------

_LOADS = re.compile(r"^load -> ", re.MULTILINE)
_VANISHED = "stored row(s) no longer in the source"


@scenario("S6", "A vanished source record is reported and kept", order=6)
def vanished(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    ids = oracle.ids(MATERIALS)
    with harness.lab("S6") as lab:
        first = lab.start_source()
        lab.start_platform()
        lab.write_config()
        sign_in(lab, record)
        run_1 = lab.cli("run", MATERIALS.source)
        record.check("run 1 exits 0", run_1.returncode == 0, run_1.stderr[:200])
        assert lab.platform is not None
        before = {
            "SQLite": read_sqlite(lab.sqlite_path, MATERIALS.table),
            "platform": read_sqlite(lab.platform.db, MATERIALS.schema),
        }
        record.check(
            f"run 1 stored all {len(ids)} items",
            all(len(rows) == len(ids) for rows in before.values()),
            str({name: len(rows) for name, rows in before.items()}),
        )
        # The same source, one item fewer: --materials 23 is the first 23 of the same seeded 24.
        first.stop()
        lab.start_source(materials=len(ids) - 1, port=first.port, label="source-without-last")
        sign_in(lab, record, force=True)
        run_2 = lab.cli("run", MATERIALS.source)
        record.check(
            "run 2, with one item removed at the source, exits 0 (no item failed)",
            run_2.returncode == 0,
            f"exit {run_2.returncode}: {run_2.stderr.strip()[-200:]}",
        )
        _check_reported_and_kept(lab, record, run_2, ids[-1], before)


def _check_reported_and_kept(
    lab: Lab, record: Recorder, run_2: Run, gone: str, before: dict[str, dict[Key, Record]]
) -> None:
    assert lab.platform is not None
    loads = _LOADS.split(run_2.stdout)[1:]
    for kind in ("sqlite", "platform"):
        name = sink_name(kind, MATERIALS)
        report = next((text for text in loads if text.startswith(f"{name} ")), "")
        record.check(
            f"the {kind} load says 1 {_VANISHED} (reported, kept)",
            f"1 {_VANISHED} (reported, kept)" in report,
            report.strip()[-160:],
        )
    after = {
        "SQLite": read_sqlite(lab.sqlite_path, MATERIALS.table),
        "platform": read_sqlite(lab.platform.db, MATERIALS.schema),
    }
    for where, rows in after.items():
        key = next(k for k in before[where] if k[1] == gone)
        record.check(
            f"{where}: the row of {gone} is still there, as it was",
            key in rows and rows[key] == before[where][key] and len(rows) == len(before[where]),
            f"{len(rows)} rows",
        )
    audited = [
        json.loads(line)
        for line in lab.cli("audit", "show", "--json").stdout.splitlines()
        if line.strip()
    ]
    absent = [e["counts"]["absent"] for e in audited if e["command"] == "load"][-3:]
    record.evidence("`absent` of the last loads in audit.jsonl", absent)
    record.check(
        "the audit log records the vanished row of the sqlite and platform loads",
        absent.count(1) >= 2,
        str(absent),
    )


# --- S7: sensitive values -----------------------------------------------------------------------

KIND_SENSITIVE = "a sensitive field's value"
KIND_ACCESS = "the access token or session the login stored"
KIND_LOGIN_KEY = "the account password"
KIND_BAD_LOGIN_KEY = "a wrong password"
KIND_USER = "the account user name"
KIND_PLATFORM_KEY = "the platform token"
KIND_BAD_PLATFORM_KEY = "a wrong platform token"
# The only places a kind of value may be found (documented: data-contract.md section 9).
ALLOWED_PLACES = {
    (KIND_SENSITIVE, "workspace/cache"),  # the raw responses, as the service sent them
    (KIND_SENSITIVE, "normalized JSONL"),  # what `normalize` wrote
    (KIND_ACCESS, "workspace/credentials"),  # the credential store, mode 0600
    (KIND_USER, "Mock source log"),  # `mock serve` announces the account it was started with
}
MIN_NEEDLE_LENGTH = 8  # a shorter value would match ordinary text by chance
MIN_SECRET_LENGTH = 20  # a token or session id; shorter pieces of the store are labels


@scenario("S7", "Sensitive values never leave the places they may be", order=7)
def sensitive(harness: Harness, record: Recorder) -> None:
    oracle = Oracle(harness.repo)
    wrong_token = f"tok-wrong-{secrets.token_hex(12)}"
    with harness.lab("S7") as lab:
        lab.start_source()
        lab.start_platform()
        lab.write_config()
        sign_in(lab, record)
        _exercise(lab, harness, record, wrong_token)
        needles = _sensitive_needles(harness, oracle, lab, record)
        needles += _credential_needles(lab, record)
        needles += [
            (KIND_LOGIN_KEY, harness.secrets.password),
            (KIND_BAD_LOGIN_KEY, harness.secrets.wrong_password),
            (KIND_USER, harness.secrets.user),
            (KIND_PLATFORM_KEY, harness.secrets.platform_token),
            (KIND_BAD_PLATFORM_KEY, wrong_token),
        ]
        found = find_values(needles, _everything_a_lab_left(lab, harness))
        _check_places(record, needles, found)


def _sensitive_needles(
    harness: Harness, oracle: Oracle, lab: Lab, record: Recorder
) -> list[tuple[str, str]]:
    """The values of the fields the *packaged mappings* mark sensitive.

    Which fields: the installed mapping, read by the tool's own loader, and the sidecar `normalize`
    wrote must agree. The values: the normalized records, each confirmed by the seeded catalogue."""
    from catalog_sync.normalize.mapping import load_mapping

    needles: list[tuple[str, str]] = []
    marked: dict[str, list[str]] = {}
    for entity in ENTITIES:
        packaged = (
            harness.installed.site_packages
            / "catalog_sync/plugins/mock/mappings"
            / f"{entity.name}.yaml"
        )
        normalized = lab.work / f"{entity.name}.normalized.jsonl"
        marked[entity.name] = sorted(load_mapping(packaged).sensitive_fields)
        record.check(
            f"{entity.name}: the sidecar of the normalized file names the same sensitive fields "
            "as the packaged mapping",
            sorted(map(str, _listed(read_sidecar(normalized).get("sensitive", []))))
            == marked[entity.name],
        )
        for (_, source_id), row in read_jsonl(normalized).items():
            for name in marked[entity.name]:
                value = row.get(name)
                if isinstance(value, str) and len(value) >= MIN_NEEDLE_LENGTH:
                    needles.append((KIND_SENSITIVE, value))
                    record.check(
                        f"{entity.name} {source_id}: {name} is the seeded catalogue's value",
                        oracle.source_attribute(entity, source_id, name) == value,
                    )
    record.evidence("fields the packaged mappings mark sensitive", marked)
    distinct = {value for _, value in needles}
    record.check(
        "the packaged mappings mark at least one field sensitive, and there are values to look for",
        len(distinct) > 0,
        f"{len(distinct)} distinct value(s)",
    )
    return needles


def _listed(value: object) -> list[object]:
    assert isinstance(value, list), value
    return value


def _exercise(lab: Lab, harness: Harness, record: Recorder, wrong_token: str) -> None:
    """Every kind of command that prints something, plus the two ways a secret could be echoed:
    a refused login and a refused load."""
    for entity in ENTITIES:
        done = lab.cli("run", entity.source)
        record.check(f"{entity.name}: run exits 0", done.returncode == 0, done.stderr[:200])
    runs = run_ids(lab)
    for command in (("runs", "list"), ("runs", "show", runs[0]), ("audit", "show", "--json"),
                    ("auth", "status", "mock"), ("inspect", MATERIALS.source)):  # fmt: skip
        lab.cli(*command)
    refused_login = lab.cli(
        "login", "mock", "--form", "--force", env={PASSWORD_ENV: harness.secrets.wrong_password}
    )
    record.check(
        "a login with the wrong password is refused", refused_login.returncode == EXIT_AUTH
    )
    for entity in ENTITIES:
        normalized = lab.work / f"{entity.name}.normalized.jsonl"
        done = lab.cli("normalize", entity.source, "--out", normalized)
        record.check(f"{entity.name}: normalize exits 0", done.returncode == 0)
    normalized = lab.work / "suppliers.normalized.jsonl"
    refused_load = lab.cli(
        "load",
        "--sink",
        sink_name("platform", SUPPLIERS),
        "--input",
        normalized,
        env={PLATFORM_TOKEN_ENV: wrong_token},
    )
    record.check(
        "a load with the wrong platform token is refused (non-zero exit)",
        refused_load.returncode != 0,
        f"exit {refused_load.returncode}",
    )


def _credential_needles(lab: Lab, record: Recorder) -> list[tuple[str, str]]:
    """What the login stored: the bearer token and the like, read from the credential store."""
    stored = (lab.home / "credentials" / "mock.cred").read_text(encoding="utf-8")
    pieces = {
        piece.rsplit("=", 1)[-1]
        for line in stored.splitlines()
        for piece in re.split(r"[\s;]+", line.partition("=")[2])
    }
    needles = [(KIND_ACCESS, piece) for piece in pieces if len(piece) >= MIN_SECRET_LENGTH]
    record.check(
        "the credential store holds an access secret to look for (a positive control)",
        bool(needles),
        f"{len(needles)} value(s)",
    )
    return needles


def _everything_a_lab_left(lab: Lab, harness: Harness) -> list[tuple[str, bytes]]:
    blobs: list[tuple[str, bytes]] = [
        ("command output", f"{run.stdout}\n{run.stderr}".encode()) for run in lab.commands
    ]
    blobs += tree_blobs(lab.home, "workspace")
    blobs += tree_blobs(lab.state, "platform and process files")
    for path in sorted(lab.work.iterdir()):
        place = (
            "normalized JSONL" if path.name.endswith(".normalized.jsonl") else f"work/{path.name}"
        )
        blobs.append((place, path.read_bytes()))
    for path in sorted((harness.out / "logs").glob(f"{lab.name}*.log")):
        blobs.append((_log_place(path.name.removeprefix(lab.name)), path.read_bytes()))
    return blobs


def _log_place(suffix: str) -> str:
    """A log of the lab: `-source.log`, `-platform.log`, or the transcript (`.log`: the commands'
    output, written by the harness, which masks the throw-away credentials in it)."""
    return {"-source.log": "Mock source log", "-platform.log": "platform log"}.get(
        suffix, "command transcript (masked)"
    )


def _check_places(
    record: Recorder, needles: list[tuple[str, str]], found: dict[tuple[str, str], int]
) -> None:
    record.evidence(
        "where each kind of value was found (distinct values)",
        {f"{kind} -> {place}": count for (kind, place), count in sorted(found.items())},
    )
    kinds = sorted({kind for kind, _ in needles})
    for kind in kinds:
        places = {place for (found_kind, place) in found if found_kind == kind}
        allowed = {place for (allowed_kind, place) in ALLOWED_PLACES if allowed_kind == kind}
        record.check(
            f"{kind}: found " + ("only in " + ", ".join(sorted(allowed)) if allowed else "nowhere"),
            places <= allowed,
            f"found in: {', '.join(sorted(places)) or 'no place'}",
        )
    total = len({value for kind, value in needles if kind == KIND_SENSITIVE})
    for place in ("workspace/cache", "normalized JSONL"):
        record.check(
            f"documented behaviour: all {total} sensitive values are present in {place} "
            "(treat the workspace and every normalized file as that sensitive)",
            found.get((KIND_SENSITIVE, place)) == total,
            f"{found.get((KIND_SENSITIVE, place), 0)} of {total}",
        )


# --- S8: refusals -------------------------------------------------------------------------------


@scenario("S8", "Refusals: no declaration, wrong password, expired token", order=8)
def refusals(harness: Harness, record: Recorder) -> None:
    _refuse_without_declaration(harness, record)
    _refuse_wrong_password(harness, record)
    _refuse_expired_token(harness, record)


def _refuse_without_declaration(harness: Harness, record: Recorder) -> None:
    with harness.lab("S8-gate") as lab:
        source = lab.start_source()
        lab.write_config(with_compliance=False, sinks=("sqlite",))
        commands: list[tuple[str, ...]] = [("login", "mock", "--form")]
        for entity in ENTITIES:
            commands += [(name, entity.source) for name in ("pull", "inspect", "run")]
        for command in commands:
            refused = lab.cli(*command)
            record.check(
                f"no compliance block: `{' '.join(command)}` exits {EXIT_COMPLIANCE}",
                refused.returncode == EXIT_COMPLIANCE
                and "no compliance declaration" in refused.stderr,
                f"exit {refused.returncode}",
            )
        record.check(
            "no compliance block: zero requests reached the source",
            source.requests() == [],
            f"{len(source.requests())} request(s)",
        )


def _refuse_wrong_password(harness: Harness, record: Recorder) -> None:
    with harness.lab("S8-login") as lab:
        source = lab.start_source()
        lab.write_config(sinks=("sqlite",))
        refused = lab.cli(
            "login", "mock", "--form", env={PASSWORD_ENV: harness.secrets.wrong_password}
        )
        record.check(
            f"wrong password: login exits {EXIT_AUTH}",
            refused.returncode == EXIT_AUTH,
            f"exit {refused.returncode}",
        )
        record.check(
            "wrong password: the source saw exactly one request, the login, and refused it",
            [(r.method, r.path, r.status) for r in source.requests()]
            == [("POST", "/api/login", 401)],
            str([(r.method, r.path, r.status) for r in source.requests()]),
        )
        for entity in ENTITIES:
            pulled = lab.cli("pull", entity.source)
            record.check(
                f"wrong password: {entity.name} pull exits {EXIT_AUTH} (no credential was stored)",
                pulled.returncode == EXIT_AUTH,
                f"exit {pulled.returncode}",
            )
        record.check(
            "wrong password: no further request reached the source", len(source.requests()) == 1
        )


def _refuse_expired_token(harness: Harness, record: Recorder) -> None:
    """An expired token is exit 2 both when the program knows it is dead before asking and when
    the service says so mid-run; neither retries."""
    _token_expired_before_the_pull(harness, record)
    _token_runs_out_during_the_pull(harness, record)


def _token_expired_before_the_pull(harness: Harness, record: Recorder) -> None:
    with harness.lab("S8-expiry") as lab:
        source = lab.start_source(token_ttl=TOKEN_TTL_S)
        lab.write_config(sinks=("sqlite",))
        record.evidence("token lifetime (seconds)", TOKEN_TTL_S)
        sign_in(lab, record)
        time.sleep(TOKEN_TTL_S + 1.0)
        for entity in ENTITIES:
            mark = source.mark()
            pulled = lab.cli("pull", entity.source)
            record.check(
                f"expired token, known before the pull: {entity.name} pull exits {EXIT_AUTH}",
                pulled.returncode == EXIT_AUTH,
                f"exit {pulled.returncode}",
            )
            record.check(
                f"expired token, known before the pull: {entity.name} made zero requests and "
                "says how to log in again",
                source.since(mark) == [] and "catalog-sync login mock" in pulled.stderr,
                f"{len(source.since(mark))} request(s)",
            )


def _data_requests_since_last_login(requests: Sequence[Request]) -> int:
    """How many data requests the source counted against the latest login (a login that checks its
    token with a request of its own has used some of its quota before the pull starts)."""
    logins = [index for index, request in enumerate(requests) if request.kind == "login"]
    after = requests[logins[-1] + 1 :] if logins else requests
    return sum(1 for request in after if request.kind in ("list", "detail"))


def _token_runs_out_during_the_pull(harness: Harness, record: Recorder) -> None:
    """A fresh login that the service refuses after N data requests, whatever the clock says."""
    with harness.lab("S8-midrun") as lab:
        source = lab.start_source(expire_after=EXPIRE_AFTER_REQUESTS)
        lab.write_config(sinks=("sqlite",))
        record.evidence(
            "data requests a login works for (mock serve --expire-after)", EXPIRE_AFTER_REQUESTS
        )
        for entity in ENTITIES:
            sign_in(lab, record, force=True)
            used = _data_requests_since_last_login(source.requests())
            mark = source.mark()
            pulled = lab.cli("pull", entity.source)
            statuses = [r.status for r in source.since(mark)]
            record.evidence(f"{entity.name}: statuses of the pull whose login ran out", statuses)
            record.check(
                f"token expiring mid-run: {entity.name} pull exits {EXIT_AUTH}",
                pulled.returncode == EXIT_AUTH,
                f"exit {pulled.returncode}",
            )
            record.check(
                f"token expiring mid-run: {entity.name} got the requests the login had left, "
                "then exactly one 401, which was its last request: no retry",
                statuses == [HTTP_OK] * (EXPIRE_AFTER_REQUESTS - used) + [HTTP_UNAUTHORIZED],
                f"{statuses}; the login itself had used {used} of {EXPIRE_AFTER_REQUESTS}",
            )


# --- S9: the real workspace ---------------------------------------------------------------------


@scenario("S9", "The real default workspace is untouched", order=10)
def real_workspace(harness: Harness, record: Recorder) -> None:
    for path in real_workspace_locations():
        before = harness.real_workspaces_before[path]
        found = changes(before, fingerprint(path))
        shown = "~" + str(path).removeprefix(str(Path.home()))
        record.check(
            f"{shown} is unchanged",
            not found,
            "did not exist before, still does not"
            if before is None and not found
            else "; ".join(found[:5]),
        )
    default_in_fake_home = harness.fake_home / ".local/share/catalog-sync"
    record.check(
        "no command fell back to the default workspace (HOME was a throw-away directory)",
        not default_in_fake_home.exists(),
    )
    ran = [lab for lab in harness.labs if lab.commands]
    if ran:  # a positive control: the commands did write somewhere, and it was their own home
        used = [lab.name for lab in ran if lab.home.exists()]
        record.check(
            "the commands wrote to their own CATALOG_SYNC_HOME",
            bool(used),
            f"{len(used)} of {len(ran)} workspace(s) created",
        )
    else:
        record.evidence("positive control", "no other scenario ran, so there was nothing to write")
    record.evidence(
        "what was fingerprinted (stat only; no file was opened)",
        [str(p) for p in real_workspace_locations()],
    )


# --- postgres ----------------------------------------------------------------------------------


@scenario("PG", "Postgres: the same profiles load into a real Postgres", order=9)
def postgres(harness: Harness, record: Recorder) -> None:
    dsn = harness.postgres_dsn
    if dsn is None:
        raise NotRun("no DSN")
    oracle = Oracle(harness.repo)
    with postgres_schema(dsn, harness.postgres_schema), harness.lab("PG") as lab:
        lab.start_source()
        lab.start_platform()
        lab.write_config(sinks=(*SINK_KINDS, "postgres"))
        sign_in(lab, record)
        for entity in ENTITIES:
            _load_into_postgres(lab, oracle, entity, dsn, harness.postgres_schema, record)


def _load_into_postgres(
    lab: Lab, oracle: Oracle, entity: Entity, dsn: str, schema: str, record: Recorder
) -> None:
    assert lab.source is not None
    expected = len(oracle.ids(entity))
    sink = sink_name("postgres", entity)
    runs = [lab.cli("run", entity.source) for _ in (1, 2)]
    for number, done in enumerate(runs, start=1):
        record.check(
            f"{entity.name}: run {number} exits 0", done.returncode == 0, done.stderr[:200]
        )
    counts = [load_counts(done.stdout).get(sink) for done in runs]
    record.evidence(
        f"{entity.name}: Postgres load (inserted, updated, unchanged), run 1 and 2", counts
    )
    record.check(
        f"{entity.name}: run 1 inserted all {expected} rows into Postgres, run 2 changed none",
        counts == [(expected, 0, 0), (0, 0, expected)],
        str(counts),
    )
    normalized = lab.work / f"{entity.name}.normalized.jsonl"
    lab.cli("normalize", entity.source, "--out", normalized)
    fields = owned_fields(read_sidecar(normalized))
    stores = {
        "normalized JSONL": read_jsonl(normalized),
        "SQLite": read_sqlite(lab.sqlite_path, entity.table),
        "Postgres": read_postgres(dsn, entity.table, schema),
    }
    record.evidence(f"{entity.name}: rows per store", {n: len(rows) for n, rows in stores.items()})
    record.check(
        f"{entity.name}: Postgres and SQLite hold the same {expected} rows",
        {len(rows) for rows in stores.values()} == {expected},
        str({n: len(rows) for n, rows in stores.items()}),
    )
    comparison = compare_stores(stores, "normalized JSONL", fields)
    record.evidence(
        f"{entity.name}: field comparison (Postgres)", {**comparison.evidence(), "owned": fields}
    )
    record.check(
        f"{entity.name}: every owned field agrees across the normalized JSONL, SQLite and Postgres",
        comparison.is_equal,
        f"{comparison.records} records, {comparison.fields} field values compared, "
        f"{len(comparison.mismatches)} mismatch(es)",
    )
    sample = sample_ids(oracle.ids(entity))
    wrong = oracle_mismatches(
        oracle, entity, lab.source.base_url, {"Postgres": stores["Postgres"]}, sample
    )
    record.check(
        f"{entity.name}: a fixed sample of {len(sample)} ids matches the oracle in Postgres",
        not wrong,
        f"{len(wrong)} mismatch(es)",
    )
    withheld = sorted(str(n) for n in _listed(read_sidecar(normalized).get("sensitive", [])))
    columns = {column for row in stores["Postgres"].values() for column in row}
    record.check(
        f"{entity.name}: the sensitive fields {withheld or '(none)'} have no column in Postgres "
        "(the drop policy)",
        not columns & set(withheld),
    )
