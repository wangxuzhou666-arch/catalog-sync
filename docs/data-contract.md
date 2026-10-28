# Data contract: what a record is and what happens to it

This page is the output contract of catalog-sync: the entity types and their fields, the normalized
file, how a record is traced back to the raw response it came from, what an empty value means, what
happens to a record that has a problem and to an item that could not be fetched, and exactly what a
load changes in a target. It describes the protocol as the code implements it. The wire format of
the HTTP sink is in [platform-contract.md](platform-contract.md) and is not repeated here.

Every claim about an output is shown in a real run, in [`examples/contract/`](../examples/contract/):
a Mock SaaS and the reference platform on loopback, the real CLI, a SQLite sink and the HTTP sink,
for both shipped entities. `tools/make_contract_example.py` generates it and a test fails if it
differs from the code. Paths below that start with `examples/contract/` are files of that run.

1. [Entities and schema versions](#1-entities-and-schema-versions)
2. [The normalized file](#2-the-normalized-file)
3. [Identity: `source` and `source_id`](#3-identity-source-and-source_id)
4. [Provenance](#4-provenance)
5. [Field reference](#5-field-reference)
6. [Missing, null, empty and unparseable](#6-missing-null-empty-and-unparseable)
7. [Issue codes](#7-issue-codes)
8. [From raw response to record](#8-from-raw-response-to-record)
9. [Sensitive fields](#9-sensitive-fields)
10. [Failures and isolation](#10-failures-and-isolation)
11. [Owned fields and keep-existing](#11-owned-fields-and-keep-existing)
12. [Re-import, retry and vanished records](#12-re-import-retry-and-vanished-records)
13. [Backups and the audit log](#13-backups-and-the-audit-log)
14. [Known sharp edges](#14-known-sharp-edges)

## 1. Entities and schema versions

A record is an instance of an **entity**, described by a schema file (YAML). Two ship with the tool:

| Entity | Schema | Version | Used by |
|---|---|---|---|
| `material` | `src/catalog_sync/schemas/material.yaml` (built in; the default of a mapping that names no `schema:`) | `schema_version: 1` | the material mappings |
| `supplier` | `src/catalog_sync/plugins/mock/schemas/supplier.yaml` (registered by the Mock plugin) | `schema_version: 1` | `src/catalog_sync/plugins/mock/mappings/suppliers.yaml` |

You can write your own schema file next to a mapping and name its path in `schema:`. The entity name, the
schema version and the key are written into every normalized file and sent in every HTTP request
(`schema.entity`, `schema.version`).

**`schema_version`** is one integer per schema, copied into every record as the field
`schema_version`. The schema file's own comment is the rule: bump it **when a field changes
meaning**. This page adds the working definition used by the tool's readers and writers:

| Change | Bump? | What happens if you do not |
|---|---|---|
| A field changes meaning, unit or kind (`weight_gsm` becomes ounces) | Yes | Old and new rows are indistinguishable in a target. |
| A field is removed or renamed | Yes | A normalized file written before the change no longer reads (see below). |
| A field is added | Not required | SQL sinks add the column on the next load; old rows hold null there. |

Reading is strict: a record line with a key the schema does not know, or a `schema_version` other
than the schema's, is refused with `RecordFormatError`, so a file from before a change must be
normalized again from the cache (`normalize` is offline). A platform may refuse a version it has not
been taught (HTTP 422). There is no automatic migration of rows already stored in a target.

## 2. The normalized file

`catalog-sync normalize SOURCE --mapping M --out F.jsonl` writes two files; `catalog-sync load
--input F.jsonl` needs both. `load` refuses to run without the sidecar, because guessing the owned
set would make it overwrite fields nobody asked it to touch.

**`F.jsonl`** is JSON Lines, UTF-8, one record per line, keys in a fixed order: `source`,
`source_id`, the schema's fields in schema order, `extra`, then the provenance fields. Every key is
always present; an empty value is `null`, `[]` or `{}` (see section 6). Money is a decimal string,
never a JSON number. See `examples/contract/materials/run-1/normalized.jsonl`.

**`F.jsonl.meta.json`** (the sidecar) holds what a load needs and a bare record cannot say. See
`examples/contract/materials/run-1/normalized.jsonl.meta.json`.

| Key | Meaning |
|---|---|
| `format` | `1`. The sidecar format. |
| `source` | The mapping's `source:`, which is the source plugin's name (section 3). |
| `run_id` | The run the records were normalized from (section 4). |
| `mapping_sha256` | sha256 of the mapping rules, the schema and every code table the rules use. A changed mapping changes it. |
| `schema` | The whole schema, embedded, so a load needs nothing but these two files. A sidecar without it means `material`. |
| `record_count` | The number of record lines. `load` refuses a file whose line count differs (edited or truncated). |
| `owned` | The fields a load may write: the fields the mapping lists, plus the loaded provenance fields (`issues`, `source_version`, `raw_ref`, `fetched_at`, `schema_version`). `normalized_at` is in the file but never loaded, because it changes on every normalize and would make every row look updated. `extra` is owned when the mapping has any `extra.*` rule. |
| `keep_existing_if_null` | The subset of `owned` that keeps a stored value when the incoming one is empty (section 11). |
| `sensitive` | The fields the mapping marked `sensitive: true` (section 9). |
| `is_full_snapshot` | `true` when the records are everything the source listed: the listing finished and no item was left out of the file. Only then may a target say a stored row is gone from the source (section 12). |

## 3. Identity: `source` and `source_id`

The key of a record is the pair **(`source`, `source_id`)**. Nothing else identifies a record, and
every sink enforces it as a primary key (`PRIMARY KEY (source, source_id)` in SQLite and Postgres;
the same pair in an HTTP request).

- **`source_id`** is the id the source uses for the item. It is whatever string the connector put in
  the listing row; the tool never derives or reformats it.
- **`source`** is the **plugin's source name** (`mock.materials`), taken from the mapping's
  `source:` line and checked against the run. It is **not** the name of the `[sources.NAME]` entry in
  the config (`mock-materials`). A run records the plugin's name too.

Dedup scope, which follows from this:

- Within one target table, a (`source`, `source_id`) is one row. A second load of the same pair
  updates the row; it never inserts a second one.
- Two config entries that run the **same plugin** (say two `[sources.*]` blocks with
  `plugin = "mock.materials"` and different options) produce the same `source`. If both load into
  one table their rows collide: ids that exist in both overwrite each other, and the stored pull
  state used for incremental pulls (keyed by the same pair) is shared as well. Give each such entry
  its own table or database, or do not run one plugin twice into one target.
- Two different plugins never collide, because their `source` differs. That is why the key is a
  pair and `source_id` alone is never used.
- **If the source changes an item's id**, the tool sees a new item. The record under the new id is
  inserted, the old row is left in place (nothing is ever deleted) and, in a complete run, reported
  as no longer in the source (section 12). The tool cannot tell a renamed id from a new item.

## 4. Provenance

Every record can be traced to the raw response that produced it. The chain is:

```
record.raw_ref = "<run_id>:<source_id>"
   -> the run's cache directory   <workspace>/cache/runs/<run_id>/
        list/<item>   the listing row exactly as the service returned it
        detail/<item> the detail record
        assets/       the pictures and their refs
        manifest.jsonl  one line per blob: kind, name, sha256, size (every read verifies it)
   -> the audit line of the pull that wrote that run     (command "pull", run_id)
   -> the audit lines of normalize and load for it        (command "normalize" / "load", run_id)
```

| Field | Type | Meaning |
|---|---|---|
| `raw_ref` | text | `<run_id>:<source_id>`. The run named here is the run that **holds the item's detail and pictures**. For an item an incremental pull found unchanged this is an earlier run than the one normalized, which is the point: the row is then unchanged on load. |
| `fetched_at` | timestamp | When that run's snapshot was created (UTC). |
| `source_version` | text | The source's change marker for the item (the Mock SaaS: `updated_at`); for a source with none, a hash of the listing row. An incremental pull refetches only items whose marker changed. |
| `schema_version` | integer | Section 1. |
| `issues` | list of text | Problems found while normalizing this record (section 7). |
| `normalized_at` | timestamp | When normalize ran. In the file, not loaded. |

A run id is `<UTC time>-<6 hex>`, for example `20261027T090100Z-xxxxxx` in the example (the suffix
is random and masked there). The sidecar's `run_id` and the `run_id` of the `normalize` and `load`
audit lines are the run that was **normalized**; `raw_ref` may name an earlier one. Neither the
`raw_ref`, the sidecar nor the audit holds a URL or a token. Mind the retention: `cache purge`
deletes a run's directory, after which `raw_ref` still names it but the raw data is gone (a purge
refuses to delete a run that unchanged items still depend on).

The audit log is in section 13. A load into a target keeps `raw_ref`, `fetched_at` and
`source_version` as ordinary columns, so a row can be traced from the database alone.

## 5. Field reference

**Required.** Only the key, `source` and `source_id`, is required. The schema has no notion of a
required, non-null or enum field: every other field is nullable, and every record carries every key.
**Enumerations.** There are no schema-level enums. Where a value comes from a closed vocabulary the
mapping uses a **code table** (a YAML file mapping the source's codes to English names): a code the
table lacks becomes null plus an issue, never a guess (section 7). The Mock SaaS's fibre table is
`src/catalog_sync/plugins/mock/tables/materials.composition.yaml`. **Units** are part of the field
name (`weight_gsm`, `width_cm`, `lead_time_days`) and are listed below.

Kinds: `text`, `integer`, `real`, `timestamp` (an ISO 8601 string with an offset; the parser returns
UTC), and `json` with what it holds (`of`): `texts`, `money`, `composition`, `images`, `object`.
"When empty" is what the record holds when the source gave nothing usable.

### Identity, extra and provenance (every entity)

| Field | Kind | Unit | Notes | When empty |
|---|---|---|---|---|
| `source` | text | | Section 3. Required. | never empty |
| `source_id` | text | | Section 3. Required. | never empty |
| `extra` | `json: object` | | Values a mapping passes through untouched with `extra.<namespace>.<name>` rules, as `{namespace: {name: value}}`. A null value is left out. | `{}` |
| `issues` | `json: texts` | | Section 7. | `[]` |
| `source_version` | text | | Section 4. | `null` |
| `raw_ref` | text | | Section 4. | `null` |
| `fetched_at` | timestamp | UTC | Section 4. | `null` |
| `normalized_at` | timestamp | UTC | Section 4. Not loaded. | `null` |
| `schema_version` | integer | | Section 1. | never empty |

### Material fields

Schema `src/catalog_sync/schemas/material.yaml`.

| Field | Kind | Unit | Shape and notes | When empty |
|---|---|---|---|---|
| `code` | text | | The source's own code for the item. | `null` |
| `name` | text | | | `null` |
| `composition` | `json: composition` | percent | List of `{"fiber": text or null, "percent": number or null, "raw": text}`, one per share of the source's composition string. `fiber` is the code table's English name, `null` for a code the table lacks. `raw` is that share as written. Null as a whole when the shares add up to more than 100. | `[]` |
| `composition_raw` | text | | The composition string exactly as the source wrote it (trimmed). Kept so a null `composition` loses nothing. | `null` |
| `weight_gsm` | real | grams per square metre | `"280"`, `"280G/M2"` and `"0GSM"` all parse. | `null` |
| `width_cm` | real | centimetres | `"150"`, `"150CM"`; inches are converted (`56"` and `57/58"`, midpoint of a range) and rounded to 0.1 cm. | `null` |
| `price` | `json: money` | currency in the value | `{"amount": "12.50", "currency": "USD"}`. `amount` is a decimal **string**, non-negative; `currency` is a 3-letter upper-case code stated by the **mapping**, not read from the source. | `null` |
| `category` | text | | Not filled by the shipped Mock mapping. | `null` |
| `use_cases` | `json: texts` | | List of text, blanks dropped, duplicates removed in order. Not filled by the shipped Mock mapping. | `[]` |
| `tags` | `json: texts` | | As `use_cases`. | `[]` |
| `supplier` | text | | The supplier's name as the source gives it. | `null` |
| `folder_paths` | `json: texts` | | One `"A/B/C"` path per folder the item is in, from what the listing walk knew (`context.folder_paths`). | `[]` |
| `images` | `json: images` | | List of `{"role", "source_url", "local_path", "sha256", "width", "height", "size_bytes"}`, one per role (`thumb`, `swatch`) that has a URL or a stored picture. `source_url` is an http(s) URL with its query string removed; `local_path` is `assets/<name>`, relative to the run named by `raw_ref`; `sha256`, `width` (px), `height` (px) and `size_bytes` describe the stored file. A role with neither a URL nor a picture is left out. | `[]` |

### Supplier fields

Schema `src/catalog_sync/plugins/mock/schemas/supplier.yaml`.

| Field | Kind | Unit | Shape and notes | When empty |
|---|---|---|---|---|
| `name` | text | | | `null` |
| `country` | text | | | `null` |
| `rating` | real | none stated | A plain number. The schema states no scale; the Mock SaaS uses 3.0 to 5.0. | `null` |
| `lead_time_days` | integer | days | A whole number; `14.5` is refused, not rounded. | `null` |
| `certifications` | `json: texts` | | List of text. | `[]` |
| `contact_email` | text | | Marked `sensitive: true` by the shipped mapping (section 9); the schema does not say so, the mapping does. | `null` |
| `address` | `json: object` | | Any JSON object, passed through. Key order is not preserved. | `{}` |
| `listed_at` | timestamp | UTC | ISO 8601 **with** an offset; `2026-01-01T00:00:00Z` is stored as `2026-01-01T00:00:00+00:00`. Without an offset it is unparseable. | `null` |

## 6. Missing, null, empty and unparseable

After normalize these four cases cannot be told apart from the record, except for the last, and only
through `issues`.

| Raw value | Example (`width` of the detail response) | Record after normalize | Issue |
|---|---|---|---|
| Key **missing** | `mat-0002`: the key is not there | `width_cm: null` | none |
| Key **null** | `mat-0003`: `"width": null` | `width_cm: null` | none |
| **Empty string** (also whitespace only, `[]` and `{}` for a scalar) | `mat-0004`: `"width": ""` | `width_cm: null` | none |
| **Unparseable** | `mat-0005`: `"width": "wide"` | `width_cm: null` | `width_cm:unparseable` |

Side by side: `examples/contract/materials/cases/mat-0002.json`, `mat-0003.json`, `mat-0004.json` and
`mat-0005.json`. For the second entity: `examples/contract/suppliers/cases/sup-003.json` (`rating`
is `""`) and `sup-004.json` (`listed_at` is `"yesterday"`).

The rules, in the order the engine applies them to one mapping rule:

1. A rule lists one or more places to read (`from: [detail.width, list.width]`). They are tried in
   order and the **first usable value wins**; a blank or unparseable value in one place does not
   hide a good value in the next.
2. A value is **blank** if it is null, missing, an empty or whitespace-only string, `[]` or `{}`.
   A blank is skipped silently: no issue.
3. A non-blank value goes to the parser. If it raises, the place is skipped and remembered as
   unparseable. If a later place yields a value, the record gets that value and **no issue**. If no
   place does, the field is null and the issue `<field>:unparseable` is recorded.
4. With `zero_is_missing: true` on the rule, a parsed zero (`0`, `0.0`, a money amount of zero)
   counts as "not filled in": it is skipped, and if nothing else yields a value the field is null
   with the issue `<field>:source_zero`. Without the flag, zero is a value and is kept as `0.0`.
5. A **list-valued** field (`texts`, `composition`, `images`) with nothing is `[]`, not null; an
   `object` field is `{}`. A scalar or `money` field is `null`.
6. A value is never invented: there is no default, no fill, no model guess.

`null` and `[]`/`{}` mean "the source did not say", not "the source said it is empty". A mapping can
ask a load to keep what is stored instead of writing such a value (section 11).

## 7. Issue codes

`issues` is a list of text in the record. A code names the field and the reason and never holds the
raw value, except for the code a table lacks and a yarn count. Duplicates are removed. This is the
complete list the code can emit; a test fails if the code gains one that this table lacks.

| Code | Emitted when | The record holds |
|---|---|---|
| `<field>:unparseable` | A non-blank raw value could not be parsed and no other place gave a value. `<field>` is the record field (`width_cm`) or `extra.<namespace>.<name>`. | the field is null |
| `<field>:source_zero` | The rule has `zero_is_missing: true` and the only value was zero. | the field is null |
| `detail:missing` | The mapping reads from `detail.*` and the item has no detail record. | whatever the listing row gave |
| `composition:unknown_code:<code>` | A fibre code is not in the rule's code table. `<code>` is the code as written. | the share with `fiber: null` |
| `composition:yarn_count_ignored:<count>` | The string holds a yarn count (`30S`, `75D/72F`, `40S*40S`), which is not a share of a fibre. | the count is left out of the shares |
| `composition:sum_over_100` | The shares add up to more than 100; which one is wrong is unknowable. | `composition` is `[]`; `composition_raw` still has the string |
| `images:<role>:not_a_url` | The picture rule found values for `<role>` but none is an http(s) URL. | no entry for the role, unless a stored picture exists |
| `images:<role>:unreadable` | A stored picture file for `<role>` could not be read as an image. | the entry is kept |

A record with issues is **still a record**: it is loaded like any other (section 10). `normalize`
prints a count per issue kind; `examples/contract/materials/run-1/transcript.txt` shows it.

## 8. From raw response to record

A **mapping** (YAML) states, for each record field it owns, where the value comes from and how it is
parsed. Fields it does not list are not owned and are never written by a load. The shipped Mock
mappings are `src/catalog_sync/plugins/mock/mappings/materials.yaml` and `suppliers.yaml`.

A rule reads from three places, never merged into one object (neither payload contains the other):

| Prefix | Is |
|---|---|
| `list.<path>` | The listing row exactly as the service returned it. |
| `detail.<path>` | The detail record, after the service's envelope (`data.data`) is removed by the connector. |
| `context.<path>` | What the walk knew, such as `folder_paths`. |

A path is `a.b[0].c`. A step that is absent gives null.

| Rule key | Meaning |
|---|---|
| `from` | One path or a list; the first usable value wins (section 6). |
| `parse` | The parser. Default: the schema field's own, else by kind (`text`, `integer`, `number`, `timestamp`, `list`, `money`, `composition`, `raw` for `object`). |
| `zero_is_missing` | Section 6, rule 4. Only on number-like parsers. |
| `on_null` | `overwrite` (default) or `keep_existing`. Section 11. |
| `sensitive` | `true` marks the field for the sink policy. Section 9. Not allowed on `extra.*`. |
| `currency` | Required by `money`: the 3-letter code. |
| `scope` / `table_file` | Required by `composition`: the code table. |
| `separators` | For `split`: the characters to split on, default `,;`. |
| `role` / `roles` | For `images`: role name to the list of paths its URL may come from. |

Parsers: `text` (trimmed; a number is turned into text), `list`, `split`, `gsm`, `width_cm`,
`number`, `integer` (refuses `14.5`), `timestamp` (needs an offset, returns UTC), `money` (a plain
non-negative decimal amount), `composition`, `raw`. A parse failure never stops the
run: it becomes an issue.

**Worked example** (`mat-0001`, run 1): the raw listing row and detail response, the normalized
line, the SQLite row and the platform row, in `examples/contract/materials/cases/mat-0001.json`.
Raw `"weight": "290 gsm"` and `"width": "148"` become `weight_gsm: 290.0` and `width_cm: 148.0`;
raw `"price": "34.22"` in the detail (and `0` in the listing, which loses to it) becomes
`{"amount": "34.22", "currency": "USD"}`. The mapping's comments explain each rule.

## 9. Sensitive fields

A mapping marks a field `sensitive: true`. The policy is a **sink option**, `sensitive_fields`, so
one mapping can feed a store that keeps everything and a shared one that must not:

| Policy | What the target receives |
|---|---|
| `drop` (default) | The field is not written at all: no column is created, a stored value is not touched, and an HTTP request does not list it in `owned`. |
| `hash` | `sha256:<hex>` of the workspace salt plus the value; a null stays null. The same value hashes the same way within one workspace. The salt cannot be rotated. |
| `keep` | The value, as it is. Must be chosen explicitly. |

What the policy does **not** cover: sensitive values are stored **in plain text** in the raw cache
(`<workspace>/cache/runs/`, it is the service's response) **and in the normalized JSONL** that
`normalize` writes. The policy is applied by `load`, at the moment a batch is handed to a sink. The
sidecar's `sensitive` lists which fields would be affected. Treat the workspace and every normalized
file as sensitive as the most sensitive field in them, and purge the cache (`cache purge`) when its
retention is over. Sensitive values are kept out of logs, error messages and the audit log.

Shown for the supplier `contact_email`: `examples/contract/suppliers/cases/sup-001.json` has the
plain value in `normalized_record`, no `contact_email` in the default SQLite row and in the platform
row, and `sha256:<salted-hash>` in the row of the sink configured with `sensitive_fields = "hash"`.

## 10. Failures and isolation

There are two different things, and they are handled differently.

**A record with parse problems is loaded.** Unparseable values become null and each one is named in
the record's `issues` (section 7). The record is written to the target like any other. Whether you
want such rows in your target is a decision for the consumer: select on `issues`.

**An item that could not be fetched is isolated.** If one item's detail or picture request fails in
a way confined to that item (for example HTTP 404 on its detail), the run does not stop:

- the item's stage is recorded as `failed` with its reason (secrets masked), the run continues with
  the other items, and the item is **not rebuilt from the listing row**: building it from the row
  alone would turn every detail-derived field into null and a load would write those nulls over good
  values;
- `normalize` leaves the item out of the file (it prints `skipped <id>: not fully pulled (...)`) and
  a load never sees it, so nothing is inserted or changed for it;
- a run with a skipped item is not a complete snapshot (`is_full_snapshot` is false), so no stored
  row is reported as gone from the source;
- the item is retried: by `pull --resume <run_id>` in the same run, or by the next pull (an item
  that never finished pulling counts as changed).

There is no quarantine store in v0: the isolation above is the whole model. Errors that are not
confined to an item (an expired credential, a refused declaration, a changed response shape) stop
the run and leave it resumable. Too many consecutive failures stop it too (exit 3, the fuse).

Exit codes of a run with failed items: `pull` exits **5** on any failed item; `run` exits **5** when
the failed items are no more than `--max-failed-ratio` of those it tried to fetch (default 0.10, or
the profile's `max_failed_ratio`) and **1** when more failed. In both cases what did arrive is
normalized and loaded.

**Listing the failed items.** `catalog-sync runs show RUN_ID` prints one line per failed item with
its stage and reason, after the run summary: `failed <stage> <source_id>: <reason>`. `runs list`
shows the number of failed items per run. The reason is the connector's message without the item id
(which the line already names), with secrets masked; it never holds a record's field values. The same lines are printed by `pull` and `run` on stderr (the
first five). Shown for `mat-0007`: `examples/contract/materials/run-1/transcript.txt` and
`examples/contract/materials/cases/mat-0007.json` (the 404, no normalized record, no row in SQLite or
on the platform; in run 2 the same item succeeds and is inserted).

## 11. Owned fields and keep-existing

**Owned fields only.** A load writes the fields in `owned` and nothing else. A column or field a
target added itself (a stock level, a note, a status) is never touched, whatever the incoming row
holds. Shown with `ops_note`, a column added to the SQLite table and to the platform's table after
run 1: it is still there after run 2 (`examples/contract/materials/cases/mat-0001.json`, `run_2`).
SQL sinks add a column for an owned field that has none; they never drop or retype one.

**The mapping and the sink use two names for one thing.**

| Where | Name | Form |
|---|---|---|
| Mapping rule | `on_null: keep_existing` | per field; the other value is `overwrite` (default) |
| Sidecar, sink batch, HTTP request | `keep_existing_if_null` | the **list** of the fields whose rule says `keep_existing` |

The mapping loader rejects a rule key named `keep_existing_if_null`. For `extra.*` rules, the one
`extra` field is listed, and all its rules must agree.

**Semantics**, for an owned field listed in `keep_existing_if_null`:

| Incoming value | Stored value | Result |
|---|---|---|
| any non-null, non-empty value | anything | the incoming value is written |
| `null` | non-null | the stored value is **kept** |
| `[]` or `{}` (a `json` field) | non-null and not `[]`/`{}` | the stored value is **kept** |
| `null`, `[]` or `{}` | `null`, `[]` or `{}` | the incoming value is written |
| `0`, `0.0` or `""` | anything | written: these are **values**, not gaps |

The zero rule: deciding that a zero means "not filled in" is the normalizer's job (`zero_is_missing`,
which produces null and the issue `source_zero`), not the sink's. And since `normalize` turns blank
text into null, a source `""` also keeps the old value; a hand-made file or another producer that
puts `""` in a `text` field overwrites. A field **not** listed is written as sent, including null.
Fields that a sensitive-field policy dropped are not owned, so they are not touched either.

Shown: `examples/contract/materials/cases/mat-0001.json`. In run 2 the source removed the item's
pictures, so the normalized record has `images: []`; the mapping says `on_null: keep_existing`, so
SQLite and the platform still hold both pictures.

## 12. Re-import, retry and vanished records

- **Re-import.** Loading the same file again changes nothing: the report says `unchanged`. A row
  is `updated` only if an owned value differs. Because `raw_ref` and `fetched_at` are owned, a
  `pull --full` (which refetches everything into a new run) makes every row `updated`; an
  incremental pull does not, for items whose marker did not change.
- **Partial failure.** Section 10. A load itself is one transaction in SQL sinks and one request
  at a time in the HTTP sink; a failed load writes nothing (SQL) or leaves the earlier requests
  applied (HTTP, see below).
- **Retry.** A pull continues with `--resume RUN_ID` (finished work is not fetched again; failed
  items are). A load can always be run again: SQL upserts are idempotent, and every HTTP request has
  an `Idempotency-Key`, so a request that was applied is answered from the platform's stored result
  and not applied twice.
- **Partial HTTP batches.** The sink sends `batch_size` records per request, in order, one at a
  time. Requests are all-or-nothing, the load is not: if request 3 of 5 fails, requests 1 and 2 were
  applied. The error says so (`the 2 request(s) before it were applied`) and running the load again
  is safe: those two are replayed. `absent` is only reported by the request that completes the
  load. Details: [platform-contract.md](platform-contract.md).
- **A vanished record** (the source stopped returning an item). Nothing is deleted. When the run
  was a complete snapshot, the load report says `N stored row(s) no longer in the source (reported,
  kept)` and the keys are in the report; the rows stay as they were. A run with a failed item, or a
  listing that did not finish, never reports this. Shown with `mat-0008`: removed from the source
  in run 2, still in SQLite and on the platform
  (`examples/contract/materials/cases/mat-0008.json`).
- **A vanished record that comes back** is simply updated again.

## 13. Backups and the audit log

**Backup.** Every real load calls `backup()` before it writes, and a sink refuses to write
without it. A dry run writes nothing, not even a backup.

| Sink | Backup | Name and place |
|---|---|---|
| SQLite | A consistent copy of the whole database file taken with SQLite's online backup, under the write lock | `<database file name>.bak-<UTC stamp>` in the same directory; the stamp is `%Y%m%dT%H%M%S%fZ` (microseconds, so two loads never share a name) |
| JSONL, CSV | A copy of the target file | the same pattern next to the file |
| Postgres | The current version of every row the load is about to change, copied inside the load's transaction | table `<table>__backup_<timestamp>` in the same schema; it holds the pre-load state of the affected rows only |
| HTTP | None: the rows are on the platform. The receipt says so | the platform's own backups are the way back |

**Backups are never deleted by the tool** and no setting does it: not `cache purge` (which touches
the raw cache only), not a later load. Their retention is yours, and they hold the same data as the
target, so they need the same access control. An empty target has nothing to copy: the report says
`backup: nothing stored yet`.

**The audit log** is `<workspace>/audit.jsonl`, append-only, created `0600`, one JSON line per
command that touches data (`login`, `pull`, `normalize`, `load`, `run`, `cache_purge`). A line has:
`ts`, `command`, `outcome` (`ok`, `ok_with_failures`, `failed`, `dry_run`, `interrupted`,
`error:<ExceptionClass>`), `source`, `run_id`, `profile`, `counts` (integers only), the sha256 of
the compliance declaration in force, a hash of the credential's token id, and the sinks written.
It never holds a token, a header, a URL, a field value or any personal data, so it records **that**
a load happened and how many rows it inserted, updated, left unchanged and found absent, and not
**which** rows or what was in them. `examples/contract/audit.jsonl` is the audit of the example run.
To find what a run did to a row, go through `raw_ref` to the cache; to find which load wrote it, match
the sidecar's `run_id` and the sink name against the `load` lines.

## 14. Known sharp edges

Behaviours the code has today that this contract documents rather than changes:

- `source` is the plugin name, so two entries of one plugin collide in one table (section 3).
- There is no required, non-null or enum concept in a schema; the only constraint is the key
  (section 5).
- Sensitive values are plain in the raw cache and in the normalized JSONL (section 9).
- A failed picture or detail request isolates the whole item, not only that field (section 10).
- `schema_version` has a one-line rule in the schema comment; the table in section 1 is the working
  definition, not something the code enforces beyond rejecting a mismatching file.
- Pictures the Mock SaaS draws are encoded by Pillow, so the digests and sizes in the example follow
  the pinned Pillow version.
