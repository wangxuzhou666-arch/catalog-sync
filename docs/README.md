# catalog-sync user guide

[Project overview](../README.md) · Full configuration, commands and examples.

English | [Chinese (zh-CN)](README.zh-CN.md)

**Bring data from the services you use into the platform you build.**

Collect authorized external data, clean and map its fields, and load the results into your own
database or business application. Run the same workflow again when the source changes.

Built for technical teams connecting SaaS or enterprise data services to their own platforms.
Start with product/material and supplier records. With Docker running, one command collects, cleans
and imports the synthetic data, then opens a report. Adapt the source and destination after trying it.

**[Try the demo](#try-the-synthetic-demo)** · [See one record end to end](#one-record-end-to-end) ·
[Connect your own service](#connect-your-own-service) · [Run in Docker](../docs/deploy.md)

> **v0.1.0rc1 · release candidate.** Includes the framework, synthetic source, offline replay and
> a reference HTTP receiver. Real services need an installed or custom Python connector.
> Tested scope and limitations: [acceptance](../docs/acceptance.md).

## Why

Access to an external service is only the beginning. Your application needs consistent records,
repeatable imports and clear rules for updating data it already owns.

| When your team needs to… | catalog-sync provides… | What that means for the workflow |
|---|---|---|
| Turn external fields into application records | Explicit field mappings, unit parsing and source-specific code tables | Keep transformation rules in a repeatable configuration. |
| Recover from an interrupted collection | Saved progress and resumable pulls | Continue from completed work. |
| Change a mapping after collection | Retained raw responses and offline normalization | Reprocess data without fetching it again. |
| Import another batch into an existing platform | Record identity, update policies and owned-field boundaries | Repeat imports while preserving application-owned fields. |
| Investigate an incomplete run | Item failures, parsing issues and audit records | Identify what needs attention before the next run. |

The complete path:

```
Authorized SaaS / enterprise service
               ↓
        Source connector
               ↓
           Raw cache  ← replay when mappings change
               ↓
     Clean · map · handle empty values
               ↓
       Normalized records
               ↓
   Your database / application receiver
```

You configure the source, field rules and destination. The framework carries the records through
that path; the [data contract](../docs/data-contract.md) defines empty values, failures and update behavior.

## One record, end to end

This is a **synthetic example**, taken from the checked [contract fixtures](../examples/contract/materials/cases/mat-0002.json).
The same source identity follows the record from the service to the receiver.

| Field | Source response | Normalized record | Stored result |
|---|---|---|---|
| Identity | `id: "mat-0002"` | `source_id: "mat-0002"` | Same source identity in SQLite and the reference receiver |
| Weight | `weight: "250 gsm"` | `weight_gsm: 250.0` | Numeric weight in grams per square metre |
| Width | Not supplied | `width_cm: null` | An explicit empty value |
| Price | `price: "38.87"`, `currency: "USD"` | `price: {amount: "38.87", currency: "USD"}` | Amount and currency remain together |

**Missing values are not guessed.** Mappings can try source fields in order. If none supplies a usable
value, the result stays empty for its field type. Unparseable values without a usable fallback leave
an entry in `issues`. A destination field marked `keep_existing` can retain its previous value when
the incoming value is empty.

<a id="try-it-in-60-seconds"></a>

## Try the synthetic demo


### Docker: one command

Install and start Docker, download or clone this repository, then run this command from its root.
Use a POSIX shell on macOS or Linux, or WSL with Docker integration on Windows.
No host Python, service account or manual demo configuration is needed.

```console
$ sh tools/demo.sh
```

The first run builds the image and downloads dependencies. The demo then runs with external network
access disabled: it starts the synthetic source and reference receiver, collects and normalizes the
data, imports it into SQLite and the receiver, and repeats the imports to verify the result.

**What you see:** a local HTML report with source values, normalized fields, imported records and
verified counts: **24 materials, 9 suppliers, 24 matching receiver records**. Both material destinations
are checked against the normalized records. Repeat imports must leave them unchanged, or the command
fails. The report opens automatically when a desktop opener is available; its path is always printed.

**Cleanup:** the launcher removes its temporary container and data volume on exit, including after a
failed run. Only the report folder and cached `catalog-sync:demo` image remain. Delete the printed
report folder when finished; `docker image rm catalog-sync:demo` removes the cached image.
The original raw cache and synthetic login state are not exported to your computer.

To choose an empty report directory or disable automatic opening, set `DEMO_OUTPUT_DIR` or
`DEMO_OPEN_REPORT=0`. Existing non-empty output directories are refused. The optional
[Compose stack](../docs/deploy.md) covers the separate Postgres and scheduled deployment example.

<details>
<summary>Alternative: run each CLI step yourself</summary>

### Step-by-step CLI

**You need:** Python 3.11–3.13, `uv`, and a fresh checkout of this repository. Open a terminal at the
repository root. Keep ports `8765` and `8780` free. The optional queries below use the `sqlite3` CLI.

The demo starts a local synthetic SaaS and a reference HTTP receiver. It collects 24 materials and
9 suppliers, normalizes their fields, and loads materials into both SQLite and the receiver.
No real service account is needed. The example configuration already contains the demo declaration.

The next block copies `catalog-sync.example.toml` to `catalog-sync.toml`, so use a fresh checkout
without your own config. For an isolated demo, set `CATALOG_SYNC_HOME` to a new local directory
outside cloud-synced folders before running it. The commands stop both demo services at the end.
The `$` characters are shell prompts; copy the commands without them.

<!-- ci:run -->
```console
$ uv sync
$ cp catalog-sync.example.toml catalog-sync.toml
$ uv run catalog-sync mock serve --detach --pid-file mock.pid
$ uv run catalog-sync mock platform serve --detach --pid-file platform.pid --db out/platform.db
$ uv run catalog-sync login mock --form
$ uv run catalog-sync pull mock-materials
$ uv run catalog-sync normalize mock-materials --out out/materials.jsonl
$ uv run catalog-sync load --sink mock-materials-db --input out/materials.jsonl
$ PLATFORM_TOKEN=demo-platform-token uv run catalog-sync load --sink mock-platform --input out/materials.jsonl
$ uv run catalog-sync run mock-suppliers
$ uv run catalog-sync audit show
$ uv run catalog-sync mock stop --pid-file platform.pid
$ uv run catalog-sync mock stop --pid-file mock.pid
```

<details>
<summary>What each command does</summary>

What the steps do:

1. `mock serve --detach` starts the service in the background and returns once it is listening;
   `mock stop` ends it. Its data is seeded and fake: 24 materials in a folder tree and 9 suppliers.
2. `login mock --form` posts the demo account to the service's login and stores the bearer token it
   answers with, in the workspace, readable by you only. No browser is involved, so CI can do it.
3. `pull` walks the folder tree and fetches every material's detail record and its two pictures,
   keeping the raw responses. `normalize` turns them into records of the built-in `material` schema,
   offline, with the mapping the connector ships inside its own package: no `--mapping`, and no copy of
   the source tree, is needed, so this block also works from an installed wheel. `load` writes them into SQLite (`{workspace}/out/mock.db`: a sink path that starts with
   `{workspace}/` is resolved against the workspace, see below).
4. `mock platform serve --detach` started the reference platform, which stores into `out/platform.db`
   (an ordinary path, relative to where you run the command).
   `load --sink mock-platform` sends the same records to it through the `http` sink, as batches of
   JSON ([the HTTP sink](#your-platform-the-http-sink)). The token is read from the environment variable
   the sink names; the demo token is the one the receiver accepts when it was given none. Its table is
   named after the entity (`material`), not after the SQLite sink's default (`materials`).
5. `run mock-suppliers` does the same three steps in one command for the second entity, which has a
   different schema (`supplier`), a different kind of pagination (a next-page token) and no pictures.
6. `audit show` lists what was done: counts and ids, never content or tokens.

</details>

**Inspect the result.** Query the databases after the demo services stop:

```console
$ WORKSPACE="${CATALOG_SYNC_HOME:-$HOME/.local/share/catalog-sync}"
$ sqlite3 -header "$WORKSPACE/out/mock.db" "select source_id, code, name, weight_gsm from materials limit 3"
$ sqlite3 -header "$WORKSPACE/out/mock.db" "select source_id, name, country, rating from suppliers limit 3"
$ sqlite3 -header out/platform.db "select source_id, code, name, weight_gsm from material limit 3"
```


**A successful first trial:** the local database contains 24 materials and 9 suppliers; the reference
receiver contains the same 24 materials. Inspect a `source_id` in both stores and compare its mapped
fields. These are expected counts for the default synthetic dataset, not a performance benchmark.

To check repeat imports, run the same `load --sink mock-materials-db` command again against the same
normalized file, then query the database: the material count should remain 24. Use the
[acceptance scenarios](../docs/acceptance.md) to reproduce interruption recovery and field preservation.

A supplier's e-mail address is marked `sensitive` in its mapping, so it is not in the database: a sink
drops sensitive fields unless it is told to hash or keep them.

State goes to `~/.local/share/catalog-sync` (the cache, the state database, credentials and the audit
log) unless you set `CATALOG_SYNC_HOME` or pass `--workspace`. It is outside the repository on purpose:
the cache holds other people's data.

The file sinks write there too. A sink `path` that starts with `{workspace}/` is resolved against the
workspace in use, so the records of a run do not end up in the checkout; any other relative path is
resolved against the config file, as before. Only two files of the demo are ordinary paths, because they
are command-line arguments and not sinks: `out/materials.jsonl` (the `normalize --out` file) and
`out/platform.db` (the reference platform's own database). Both hold only the Mock's invented data, and
`out/` is git-ignored. For a real source, give `--out` a path in the workspace as well.

**Never point sinks at a cloud-synced folder (iCloud/Dropbox/OneDrive).** The records are other people's
data, which a sync client would copy to servers you did not choose, and it can copy a SQLite file in the
middle of a write.

### Offline: replay a stored snapshot

The repository also ships a synthetic snapshot (`examples/synthetic`, made of obviously fake data by
`tools/make_synthetic.py`). The `replay` source serves it instead of calling an API, so not even a
local server is needed. This block is run by the test suite on every change too. Unlike the block above it
needs a checkout of the repository: the snapshot and the mapping the `demo` profile names are files in it,
not in the installed package.

<!-- ci:run -->
```console
$ uv run catalog-sync run demo
$ uv run catalog-sync runs list
$ uv run catalog-sync cache purge --dry-run
```

`run demo` pulls the snapshot, normalizes it with the Mock materials mapping, and loads three sinks:
`{workspace}/out/catalog.db` (SQLite), `{workspace}/out/catalog.jsonl` and `{workspace}/out/catalog.csv`.
`runs list` shows the runs in the workspace, and `cache purge --dry-run` what the retention policy would
delete (nothing, yet).

```console
$ WORKSPACE="${CATALOG_SYNC_HOME:-$HOME/.local/share/catalog-sync}"
$ sqlite3 -header "$WORKSPACE/out/catalog.db" "select source_id, code, name, weight_gsm from materials limit 3"
```

Run the pipeline step by step instead:

```console
$ uv run catalog-sync pull demo                      # raw responses into the workspace cache
$ uv run catalog-sync normalize mock.materials --run latest --out records.jsonl
$ uv run catalog-sync load --sink local --input records.jsonl --dry-run
$ uv run catalog-sync load --sink local --input records.jsonl
```

</details>

<a id="quick-start"></a>

## Connect your own service

Once the synthetic demo works, choose the parts you need to adapt:

| Integration step | What you supply | Guide |
|---|---|---|
| Source | Authorization and an installed or custom Python connector for a compatible interface | [Connector guide](../docs/connector-guide.md) |
| Transformation | Field mappings, code tables and empty-value policy | [Data contract](../docs/data-contract.md) |
| Destination | A configured database sink, compatible HTTP receiver or custom adapter | [Platform contract](../docs/platform-contract.md) |
| Runtime | CLI execution or a Docker deployment with your configuration | [Deployment guide](../docs/deploy.md) |

For a service with a connector already installed, `init` walks through authorization, source settings,
login, a sample pull and the destination. It does not create an integration for an unsupported service.
Run it in a separate checkout/configuration from the demo:

```console
$ uv sync
$ uv run catalog-sync init
```

What it does, in order:

1. **Pick a connector.** Every installed connector is listed with a line about it, and its own settings
   (the address of the service, say) are asked from the connector's declaration.
2. **Fill in the compliance declaration**, field by field, each with its explanation: who owns the data,
   on what authority you read it (`own_account`, `customer_authorized`, `contract`, `public_data`) and
   where that is recorded, the terms you reviewed, the request rate you will hold, how long the raw
   cache may be kept. Every answer is checked by the rules the gate applies. The review date is
   **today only once you have said you read the terms**: `init` does not pick a date for you, and
   stops, before any request, if you have not.
3. **Log in**, in a way the connector offers: an HTTP login (`--form`), a browser (`--browser`, with a
   visible window when a person has to type), or a pasted cURL command. The account comes from
   environment variables and is never typed into a file.
4. **Look before it commits.** `inspect` the source, pull a small **sample** (5 items unless you say
   otherwise) and see it **normalised** in a table, with tokens masked and `sensitive` fields left out.
5. **Choose where the records go**: SQLite, Postgres, your platform over HTTP (the
   [platform contract](../docs/platform-contract.md)), JSONL or CSV. A secret is never asked for: where a
   token or a connection string is needed you name the **environment variable** that holds it.
6. **Write `catalog-sync.toml`**, last, and print the command to run. An existing file is not
   overwritten without `--force`, and no secret is ever written to it.

The login, the sample pull and the normalisation leave their usual lines in the audit log, and `init`
adds one of its own.

What `init` does not do, so that it does not surprise you:

- **It asks only what a connector marks as required, or as something nearly everybody sets** (the
  address of the service). Every other setting keeps its default; change it in the file afterwards.
- **A connector that declares no settings is listed as such**, and `init` can ask nothing about it: its
  `[sources.NAME]` table has to be written by hand (the [example config](../catalog-sync.example.toml)
  shows the shape; the [connector guide](../docs/connector-guide.md) says how a connector declares its
  settings).
- **`--non-interactive` needs every answer.** A question with no answer and no default is an error that
  names the key. The compliance answers have no defaults on purpose: they are statements of fact that
  only you can make.
- **The sample pull is a real run.** It makes real requests to the real service, fills the raw cache and
  writes audit lines, which is why the compliance declaration comes before it.
- **It prints the `run` command and stops.** It schedules nothing: calling that command from cron, a
  systemd timer or your job runner, or [`catalog-sync schedule`](#command-reference), is yours to set up
  ([docs/deploy.md](../docs/deploy.md) shows how, with a container image and a compose stack).
- **It cannot tell whether your declaration is true**, only that it is complete and consistent
  ([what that leaves to you](../docs/compliance.md)).

For a script or CI, give the answers as a file and ask for nothing:
`catalog-sync init --answers answers.toml --non-interactive`.
[`examples/init/mock-materials.toml`](../examples/init/mock-materials.toml) shows every key; it sets up the
built-in Mock SaaS with a SQLite file and the reference platform as sinks, and the test suite runs it on
every change.

Return to the [synthetic demo](#try-the-synthetic-demo) to evaluate the workflow without a real account.

## Supported scope

| Area | First-release boundary |
|---|---|
| Entities | Material and supplier schemas; other business entities require an extension. |
| Sources | Synthetic source and offline replay included. Real SaaS or enterprise services need a compatible connector and authorized access. |
| Destinations | JSONL, CSV, SQLite, Postgres and HTTP. HTTP is validated against the included reference receiver; CSV has contract-test coverage, not the full acceptance scenarios. |
| Missing values | Explicit fallback and empty-value policies; no inferred values. |
| Access | No generic HTML scraping, JavaScript request-signature generation, CAPTCHA or unattended second-factor handling. |
| Release evidence | Original frozen candidate: 18 local acceptance checks passed. Public-candidate GitHub CI is pending; see [tested environments and provenance](../docs/acceptance.md). |

Docker packages the runtime. Source permissions, connector adaptation and receiver compatibility
still need to be configured and validated for your system.

## Share your trial feedback

Tell us your source type, destination type, the step you reached and the first blocker. Include the
release and environment used. A synthetic example or redacted schema is enough; do not include
credentials, private endpoints or real business records. Use the repository's Issues tab once available.

For deeper integration work: [configuration](#configuration-reference) · [commands](#command-reference) ·
[custom sources and sinks](#writing-your-own-source-or-sink) · [contributing](../CONTRIBUTING.md).

## How it works

```
 SaaS tool                  catalog-sync                                        your platform
 ---------    +-----------------------------------------------------+    -------------------
              |  compliance gate: declaration valid? rate ceiling?  |
   HTTP  ---> |  credential fresh?  (all checked before any call)   |
              |                                                     |
              |  collect         clean + normalize          load    |
              |  pull  ------>  raw cache  ------>  mapping  ---> sinks ---> SQLite, Postgres,
              |  (resumable)    (kept, offline)     + code tables   |        JSONL, CSV, HTTP, or yours
              |                                                     |
              |  audit log: one line per login, pull, normalize,    |
              |  load, run and purge. Counts and ids, never content.|
              +-----------------------------------------------------+
```

1. **Collect** (`pull`). A *source* plugin lists the items, fetches each one's detail record and its
   pictures, and the raw responses are stored under the workspace, one directory per run, with a
   checksum for every file. A killed or failed pull is continued with `--resume`; a second pull only
   fetches what changed. HTTP is paced (a declared ceiling per minute, enforced as a sliding window),
   retried with backoff on 5xx and 429, and never retried on other 4xx.
2. **Clean and normalize** (`normalize`). Reads only the cache, so it works offline and a changed
   mapping can be tried again at once. A YAML *mapping* states, for each field of the record, which
   raw values feed it (listing row and detail record are read side by side and never merged), how to
   parse them, and whether it is `sensitive`. Code tables are per source and never merged.
3. **Load** (`load`). A *sink* writes the records (a file, a database, or your platform over HTTP). Only the fields the mapping owns are written;
   `NULL` is written as `NULL` unless the field is marked `keep_existing`; rows are never deleted
   (rows the source no longer returns are reported); a backup is taken first; `--dry-run` shows
   what would change and writes nothing. SQL sinks use `INSERT ... ON CONFLICT DO UPDATE`, so child
   rows that cascade on delete survive a refresh.
4. **Around all of it**: the **compliance gate** refuses to start a pull, or a login that makes a
   request, without a valid declaration, and the **audit log** records what was done.

The output contract (entities and schema versions, the normalized file, the `(source, source_id)` key,
provenance, field tables, empty-value rules, issue codes, sensitive policy, failure handling, owned
fields and `keep_existing`, backups and audit) is in [docs/data-contract.md](../docs/data-contract.md). A
generated side-by-side run for materials and suppliers, from the Mock SaaS through SQLite and the
reference platform, is in [examples/contract/](../examples/contract/) and is checked against the code by a
test.

## Connectors

| Connector | What it reads | Compliance block | Status |
|---|---|---|---|
| `replay` | A stored snapshot directory: the synthetic demo, or runs you captured earlier. | Not needed (local files only); still audited. | Tested in CI. |
| `mock.materials`, `mock.suppliers` | The built-in Mock SaaS: invented data behind a login, with page-number and next-page-token pagination, a folder tree, pictures and the usual traps. | **Required** (the example config fills it in for the demo): the mock is a networked source, it makes real HTTP calls, to localhost. | Tested in CI, end to end. |

A connector for another service is written once, by a developer, and used without code by everyone
else: `catalog-sync new-connector` generates one that already passes its own tests, and
[docs/connector-guide.md](../docs/connector-guide.md) takes it from the browser's network tab to a first
live pull (see [Writing your own source or sink](#writing-your-own-source-or-sink)). Connectors for other
platforms can ship as separate packages; see [docs/connector-guide.md](../docs/connector-guide.md).

### The built-in Mock SaaS

`catalog-sync mock serve` starts a small fake service on `127.0.0.1:8765`. It uses only the standard
library and Pillow, and ships inside the package, so it is there after `pip install`. Its data is seeded
(`--seed`) and obviously fake. It exists so that anyone can try, demo and test the tool with no account
and no legal question, and it is what the connectors' end-to-end tests run against.

| Entity | Pagination | Shape |
|---|---|---|
| `materials` | page number, with a total | A folder tree. The listing row has a picture and a price of 0; the detail record (wrapped twice, `data.data`) has the price, weight, composition and a second picture, and an empty `thumbs`: neither contains the other. |
| `suppliers` | next-page token, no total | A flat list; no detail record and no pictures. |

Login is an HTML form (`/login`) or `POST /api/login`. Both give a bearer token (a signed JWT, valid for
a week unless you set `--token-ttl`) and a cookie session, and the API accepts either. Log in with
`login mock --form` (a plain HTTP request; what CI does), `login mock --browser [--headed]` (the app
page sends the token the way a single-page app does) or `login mock --paste-curl -`. The account is
`MOCK_SAAS_USER` and `MOCK_SAAS_PASSWORD`; when neither is set it is the public demo account `demo` /
`demo-password`, which protects nothing, and the server refuses to listen beyond loopback while that
password is in place.

The traps, each a flag of `mock serve` and each something a real service does:

| Flag | What the service does | What the tool does |
|---|---|---|
| `--rate-limit COUNT/SECONDS` | Answers 429 with `Retry-After` beyond the limit. | Waits as told and goes on. |
| `--error-every N` | Every Nth data request answers 503. | Retries with exponential backoff. |
| `--gone-every N` | The detail of every Nth material answers 404. | The item is marked failed, the run goes on. |
| `--expire-after N` | A login works for N data requests, then 401 until the next login. | Stops at once with exit code 2. The same as `--token-ttl`, but in the middle of a run, whatever the clock says. |
| `--token-ttl SECONDS` | The token and the session stop working: 401. | Stops at once with exit code 2 and says how to log in again. A 401 is never retried. |
| `--signed-urls SECONDS` | Picture links carry a signature that expires: 403. | That picture fails, with the reason; the rest of the run goes on, and the signature is never stored. |

The traps in the *shape* of the data are always on, because they are how the data looks: the price of 0
in the listing, the empty `thumbs` in the detail, the double envelope, a weight of 0 for "not filled in",
and a fibre code that no code table lists.

```console
$ uv run catalog-sync mock serve --rate-limit 8/5 --error-every 9 --signed-urls 120
```

## Configuration reference

One TOML file, `catalog-sync.toml` in the working directory or `--config FILE`. Relative paths in it
resolve against the file. [`catalog-sync.example.toml`](../catalog-sync.example.toml) is a complete,
commented starting point. It never holds credentials.

```toml
[workspace]
home = "~/.local/share/catalog-sync"    # cache, state database, credentials, audit log

[sources.my-source]                      # a named instance of a source plugin
plugin = "mock.materials"
credential = "mock"
# ...the plugin's own options...

[sources.my-source.compliance]           # required for any source that contacts a service
data_owner = "Example Co"
authorization = "own_account"
authorization_ref = "wiki/data-access/12"
tos_url = "https://vendor.example/terms"
tos_reviewed_on = 2026-10-26
max_requests_per_minute = 120
retention_days = 90

[sinks.warehouse]                        # a named instance of a sink kind
kind = "postgres"
dsn_env = "WAREHOUSE_DSN"

[sinks.local]
kind = "sqlite"
path = "{workspace}/out/catalog.db"      # in the workspace, not in the checkout
table = "materials"

[profiles.weekly]                        # what `catalog-sync run weekly` does
source = "my-source"
# mapping = "my-mapping.yaml"            # optional: left out, the connector's own packaged mapping is used
sinks = ["warehouse", "local"]
incremental = true
max_failed_ratio = 0.10                  # more failed items than this share of those fetched: the run fails
```

The workspace directory is resolved from, in order: `--workspace`, `$CATALOG_SYNC_HOME`, `[workspace]
home`, then `~/.local/share/catalog-sync`.

A profile's `mapping` is optional. Left out, `run` applies the mapping that the connector of the profile's
source ships inside its package (the one `init` tries out). `catalog-sync normalize SOURCE` does the same
when `--mapping` is left out; SOURCE is a connector name (`mock.materials`) or a `[sources.*]` entry, whose
`plugin` is used. A source that is no connector (`replay`, which replays any snapshot) ships no mapping:
the profile or the command has to name one, or it stops with exit code 1 and says so. A mapping you name
always wins.

### The compliance block

| Key | Meaning |
|---|---|
| `data_owner` | The organisation whose data this is. |
| `authorization` | `own_account`, `customer_authorized`, `contract` or `public_data`. |
| `authorization_ref` | Where the grant is recorded: a ticket, a wiki page, a contract number. A pointer, not the content. |
| `tos_url` | The terms of service you reviewed. |
| `tos_reviewed_on` | The date you reviewed them. Older than `tos_review_max_age_days` (default 180) prints a warning. |
| `max_requests_per_minute` | A ceiling enforced on every request of the source, retries included. A `request_gap_s` shorter than 60 divided by it is refused. |
| `retention_days` | How long the raw cache may be kept. `run` warns when it is older; `cache purge` enforces it. |

Without a complete, valid block, `pull`, `inspect` and `run` stop with exit code 4 before a credential
is read or a request made. So do `login --form` and `login --browser`, because a login posts a password
and makes requests of its own: they need the block of a source whose `credential` is the one being logged
into (with several, the lowest `max_requests_per_minute` among them paces the login), and a credential
that no source uses is refused. `login --paste-curl` sends nothing and needs none. The tool checks that
the block is there and consistent; it cannot check that it is true. What it enforces, and what stays with
you, is laid out in [docs/compliance.md](../docs/compliance.md).

### Sinks

Every sink accepts `sensitive_fields = "drop" | "hash" | "keep"` for fields a mapping marks
`sensitive: true`. `drop` (the default) does not write them at all; `hash` writes a salted sha256
that is stable within the workspace; `keep` writes them as they are and has to be chosen.

| Kind | Options | Notes |
|---|---|---|
| `jsonl` | `path` | One JSON object per line, merged by `(source, source_id)`; rewritten atomically; locked while loading. |
| `csv` | `path` | Same merge. Text that a spreadsheet would run as a formula (a cell starting with `=`, `+`, `-`, `@`, tab or CR) is written with a leading apostrophe; reading undoes it. Numbers are not touched. |
| `sqlite` | `path`, `table` (default `materials`) | `INSERT ... ON CONFLICT DO UPDATE`; additive columns; backup with the online backup API, taken inside the load's own write transaction. Loads into one file take turns: a second one waits up to 5 seconds for the first to commit, then stops with a clear error. |
| `postgres` | `dsn_env`, `table`, `schema` | Needs the extra: `pip install 'catalog-sync[postgres]'`. The connection string is read from the environment variable `dsn_env` names. Column types follow the field kinds (`text`, `bigint`, `double precision`, `jsonb`, `timestamptz`). The backup is `<table>__backup_<timestamp>`, holding the pre-load version of exactly the rows the load changes; it is a safety net for one load and does not replace `pg_dump`. A dry run runs in a transaction that is rolled back. A load takes an advisory lock per table, so two loads into one table take turns (the second waits, with no timeout); profiles that share a table should all name the `schema`, so that they lock the same one. |
| `http` | `url`, `auth_header_env`, `auth_header`, `auth_scheme`, `batch_size`, `max_requests_per_minute`, `timeout_s`, `allow_plain_http` | POSTs JSON batches to a platform you run (see [Your platform](#your-platform-the-http-sink)). Takes no backup: that is the platform's job, and the load report says so. A dry run builds the requests and sends nothing. |

The flat-file sinks (`jsonl`, `csv`) take an exclusive lock on `<file>.lock` while a load runs and a
second loader is refused at once with a clear message.

The `path` of a `sqlite`, `jsonl` or `csv` sink is resolved against the config file, except that a
path starting with `{workspace}/` is resolved against the workspace in use (`--workspace`, then
`$CATALOG_SYNC_HOME`, then `[workspace] home`, then `~/.local/share/catalog-sync`: the same order as the
state database). Use it, so that the output stays out of the checkout; `catalog-sync init` offers it by
default. `{workspace}` alone, or with nothing after the slash, is a configuration error and not a folder of
that name. **Never point sinks at a cloud-synced folder (iCloud/Dropbox/OneDrive).**

### Your platform (the HTTP sink)

The `http` sink hands the records to a platform you run, in batches, one `POST` of JSON each:

```toml
[sinks.platform]
kind = "http"
url = "https://platform.example/api/ingest"
auth_header_env = "PLATFORM_TOKEN"       # the NAME of the variable that holds the token
batch_size = 100
```

A request carries only the fields the mapping owns, the list of those that keep an existing value when
empty, the schema's name and version, and the run id; every record has an idempotency key and every
request an `Idempotency-Key` header (the sha256 of its body), so a retry or a re-run is never applied
twice. 5xx and 429 are retried with backoff, a 401 or 403 stops the run with exit code 2, and any other
4xx is reported with the start of the platform's own explanation, secrets masked. The token is read from
the environment variable `auth_header_env` names; a config that holds one is refused. Plain `http://`
is accepted for loopback only. A redirect is not followed, because the token would travel to the new
address: the sink stops and says to use the final address as `url`.

What your platform has to do (upsert owned fields only, never delete, answer idempotently) is written down
in [docs/platform-contract.md](../docs/platform-contract.md), with the exact request and answer.
`catalog-sync mock platform serve` is a reference receiver that implements it, and the same contract
suite that checks every other sink runs against it. A remote sink cannot back up what it does not hold:
recovery is the platform's job.

### Mappings

A mapping is YAML with one rule per record field it **owns**; a load never touches a field the mapping
does not list.

```yaml
version: 1
source: mock.materials
schema: material                          # what kind of record this fills; material if left out
fields:
  name:
    from: [detail.name, list.name]        # the first non-empty value wins
  weight_gsm:
    from: [detail.weight]
    parse: gsm
    zero_is_missing: true                 # a zero from the service means "not filled in"
  composition:
    from: [detail.composition]
    parse: composition
    scope: mock.saas/materials            # the code table; scopes are never merged
  folder_paths:
    from: [context.folder_paths]
    on_null: keep_existing                # an empty value keeps what the sink already holds
  supplier:
    from: [detail.some_field]
    sensitive: true                       # sinks drop it unless told otherwise
```

Values come from `list.<path>` (the listing row as received), `detail.<path>` (the detail record) and
`context.<path>` (what the walk knew, such as folder paths). Parsers: `text`, `list`, `split`, `gsm`,
`width_cm`, `number`, `integer`, `timestamp`, `money`, `composition`, `raw`. The fields a mapping can fill
are the ones its schema declares (for `material`: `code`, `name`, `category`, `supplier`,
`composition_raw`, `composition`, `weight_gsm`, `width_cm`, `price`, `use_cases`, `tags`,
`folder_paths`, `images`), and anything else as `extra.<namespace>.<name>`. A record also carries
`issues` (what could not be understood, for example `weight_gsm:source_zero` or
`composition:unknown_code:X`), the source version, a reference to the raw response, and when it was
fetched. Pictures carry their width and height, read from the cached file.

### Entity schemas

A record is whatever its entity schema says, so the same pipeline carries a fabric library, a supplier
list or a type of your own. A schema is a YAML file:

```yaml
version: 1
entity: supplier
schema_version: 1                       # written into every record; bump it when a field changes meaning
key: [source, source_id]                # sinks key every row this way
fields:
  name: {kind: text}
  rating: {kind: real}
  lead_time_days: {kind: integer}
  certifications: {kind: json, of: texts}
  listed_at: {kind: timestamp}
```

`kind` is how a sink stores the field: `text`, `integer`, `real`, `timestamp` (an ISO 8601 string with an
offset) or `json`. A `json` field says what it holds with `of`: `texts` (a list of strings), `money`,
`composition`, `images` or `object`. A field may name the parser a rule uses when it names none
(`parse: gsm`). Every record also carries `source`, `source_id`, `extra` and the provenance fields, so a
schema may not declare those.

Two schemas ship: `material` (built in, and what a mapping means when it names none) and `supplier`
(registered by the mock connector). A mapping says which it fills with `schema: supplier`, or with a path
to a schema file next to it; a plugin registers its own under the `catalog_sync.schemas` entry-point
group. A rule for a field the schema does not declare is refused when the mapping is read, before any
data is touched. A normalised file carries its schema with it, so `load` needs nothing else.

## Command reference

```
catalog-sync [--config FILE] [--workspace DIR] COMMAND

init [--answers FILE.toml] [--non-interactive] [--force]
login CREDENTIAL [--paste-curl FILE|-] [--form] [--browser [--headed]] [--force]
auth status CREDENTIAL
inspect SOURCE [--folder ID] [--option KEY=VALUE]
pull SOURCE [--stage list|detail|assets|all] [--resume RUN] [--max-items N] [--full] [--option KEY=VALUE]
normalize SOURCE --mapping FILE --out FILE.jsonl [--run RUN|latest]
load --sink NAME --input FILE.jsonl [--dry-run]
run PROFILE [--max-failed-ratio RATIO]
schedule PROFILE --every DURATION [--jitter DURATION] [--max-consecutive-failures N] [--max-failed-ratio RATIO] [--once]
health [--max-age DURATION|Nx-interval] [--profile PROFILE]
audit show [--last N] [--json]
runs list [--source SOURCE]
runs show RUN
cache purge [--older-than 30d] [--source SOURCE] [--include-running] [--dry-run]
mock serve [--host HOST] [--port N] [--seed N] [--materials N] [--suppliers N] [--token-ttl SECONDS] [--rate-limit COUNT/SECONDS] [--error-every N] [--gone-every N] [--expire-after N] [--signed-urls SECONDS] [--pid-file FILE] [--detach] [--verbose]
mock stop --pid-file FILE
mock platform serve [--host HOST] [--port N] [--db PATH] [--token-env NAME] [--pid-file FILE] [--detach] [--verbose]
new-connector NAME [--dir PATH] [--auth bearer|cookie|api_key|form] [--pagination page|token|offset] [--catalog-sync-source auto|pypi|path]
```

| Exit code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Failure: an unexpected error (its traceback is logged, masked), or a `run` in which more than `--max-failed-ratio` of the fetched items failed (what did arrive is still normalized and loaded). |
| 2 | The credential is missing, expired or could not be obtained. The message says how to renew it. |
| 3 | The failure fuse tripped (too many consecutive item failures, or more than `max_pages` pages fetched for one listing in one run). Continue with `--resume`. |
| 4 | The source's compliance declaration is missing or invalid, or `login --form` / `login --browser` found no valid declaration of a source that uses the credential. No request was made. |
| 5 | Completed with item failures: the run finished, but some items failed. `pull` exits 5 on any failed item. `run` exits 5 when the failed items are no more than `--max-failed-ratio` of those it tried to fetch (default `0.10`, or the profile's `max_failed_ratio`) and 1 above that. Items skipped as unchanged are not counted, and an item that failed in two stages counts once. |
| 130 | Interrupted. |

`cache purge` deletes the raw responses of runs older than the retention each source declared (or than
`--older-than`, in days). A run that unchanged items still depend on is **never** deleted; it is
listed as kept, with the number of items, and `pull --full` moves those items into a new run so that
the old one can go. Every purge is written to the audit log.

`schedule` is `run` on a timer, in the foreground, until SIGTERM or SIGINT (`--every 30m`, `6h`, `1d`;
`--jitter` adds a random pause of up to that much; the pause counts from the end of a cycle). It writes one
`schedule` audit line per cycle, and refuses to start when another scheduler of the same profile holds
`schedule-PROFILE.lock` in the workspace. What a cycle's outcome does to the loop:

| Cycle | Loop |
|---|---|
| Succeeds | Waits `--every`, goes again. |
| Finishes with failed items, no more than `--max-failed-ratio` of those fetched (`run` exit 5) | A success, marked **degraded** in the output and in the audit line (outcome `ok_with_failures`): no backoff, and the count of failures in a row starts again. |
| Credential expired or missing (exit 2) | Says so every cycle and keeps going: only a login on the host can fix it, and it is not counted as a failure. |
| Compliance declaration missing or invalid (exit 4) | Stops with exit 4. Not retryable. |
| Anything else, including more than `--max-failed-ratio` of the items failed | Waits 1 minute, then 2, 4, ... never more than `--every`; after `--max-consecutive-failures` in a row (default 5) it exits 1, so a supervisor restarts it visibly. |

A signal lets the cycle in progress finish (the puller has no cancellation point) and then exits 0; a second
signal interrupts the cycle where it is (exit 130), and `pull --resume` can continue the run. `--once` runs one
cycle and exits with its exit code (5 for a degraded cycle). Before each cycle it warns when a credential's `renew_before_days` is
shorter than `--every`.

`health` exits 0 when the last successful `schedule` cycle is no older than `--max-age`, else 1. A degraded
cycle counts as successful, and `health` then prints `healthy, degraded: ...` so that a person reading it
can tell. `--max-age` is a duration like `90m`, or a multiple of the interval the scheduler ran with: the
default `2x-interval` is twice the interval plus the longest jitter (the audit line records the interval
and `jitter_seconds`, so `health` needs no arguments). It reads only the audit log, which makes it fit for
a Docker `HEALTHCHECK`.

The repository ships a `Dockerfile` and a compose stack (the scheduler, Postgres, and the Mock SaaS and
reference platform to try it against), with the login done by a one-shot container, restart and stop
handling for the exit codes above, and a CI job that runs it end to end. After giving the variables of
`deploy/.env.example` a value in `deploy/.env`:

```console
$ docker compose -f deploy/compose.yaml up -d mock-saas mock-platform postgres
$ docker compose -f deploy/compose.yaml run --rm login
$ docker compose -f deploy/compose.yaml up -d
```

[docs/deploy.md](../docs/deploy.md) walks through it, then through pointing it at a real service and your own
platform, credential renewal, backups, upgrades, running under systemd instead, and the security notes.

## Writing your own source or sink

Plugins are found through Python entry points, so they can live in another package, public or private.
Core code never imports a plugin.

### A connector in an afternoon

For a service whose web app gets its data from JSON endpoints that your own login can call, do not start
from the interface below. `new-connector` writes a package that already works against a made-up
service, and this block is run by the test suite on every change:

<!-- ci:run -->
```console
$ uv run catalog-sync new-connector acme --dir out/connectors/acme
$ uv run pytest -q -c out/connectors/acme/pyproject.toml --rootdir out/connectors/acme out/connectors/acme/tests
```

The package has its own `pyproject.toml` and entry points, settings, API calls, a credential kind
(`--auth bearer|cookie|api_key|form`), a source with page-number, next-page-token or offset pagination
(`--pagination`), a schema and a mapping, a fake service with invented data, tests that run the connector
through pull and normalize against it, and a compliance block whose placeholders the gate refuses until
you replace them. Everything to change is marked `TODO(connector)`. The building blocks are public in
`catalog_sync.sdk` (options, connection, pagination, folder walk, pictures, credentials) and
`catalog_sync.testing` (a fake service on one shared clock, the dataset-as-oracle comparison). The
[connector guide](../docs/connector-guide.md) covers the whole way: finding the endpoints in the browser's
developer tools, choosing the authentication, writing synthetic fixtures (never captured responses),
filling in the compliance block, and the first live `inspect` and `pull`. It also says what the SDK
cannot do: requests signed by JavaScript, pages that are only HTML, CAPTCHAs.

### A source

```python
# my_package/source.py
from collections.abc import Iterator, Mapping
from pathlib import Path

from catalog_sync.models import JsonObject
from catalog_sync.sources.base import AssetRef, ListedItem
from catalog_sync.state import Checkpoint


class MySource:
    name = "my.source"

    @classmethod
    def from_options(cls, options: Mapping[str, object], *, base_dir: Path) -> "MySource":
        # `options` is the [sources.NAME] table. For a source that contacts a service it also
        # holds max_requests_per_minute (from the compliance block), credential (a handle), ...
        return cls()

    def enumerate(self, state: Checkpoint) -> Iterator[ListedItem]:
        # Yield every item. Record your place with state.set_frontier() so that a run can resume.
        raise NotImplementedError

    def fetch_detail(self, item: ListedItem) -> JsonObject | None:
        raise NotImplementedError

    def asset_refs(self, item: ListedItem, detail: JsonObject | None) -> list[AssetRef]:
        raise NotImplementedError

    def fetch_asset(self, ref: AssetRef) -> bytes:
        raise NotImplementedError
```

```toml
# my_package's pyproject.toml
[project.entry-points."catalog_sync.sources"]
"my.source" = "my_package.source:MySource.from_options"
```

Make requests with `catalog_sync.http.HttpClient` and a `RateLimiter(options["max_requests_per_minute"])`
(`catalog_sync.sdk.Connection` wires both) so that pacing, backoff and the declared ceiling are the same everywhere; raise `ItemFailed` for a
problem with one item, `AuthExpired` for a rejected credential, `SourceShapeError` for a response of the
wrong shape. A source that only reads local files can also register under `catalog_sync.local_sources`,
which exempts it from the compliance block. Optional: implement `inspect(folder)` to support the
`inspect` command; register code tables under `catalog_sync.code_tables`, entity schemas under `catalog_sync.schemas`
and a login method under `catalog_sync.credentials` (`form_login` makes it a plain HTTP login,
usable without a browser). `catalog_sync.plugins.mock` is a small complete example of all of it. [CONTRIBUTING.md](../CONTRIBUTING.md) lists the rules a connector must follow.

The settings of a connector (key, type, default, a line of help) are declared once, as `SettingSpec` values
next to the code that reads them, and published with a one-line summary, the credential kind and the mapping
as a `ConnectorInfo` under the `catalog_sync.connectors` entry-point group. That declaration is how
`catalog-sync init` sets a connector up without knowing its code; `new-connector` generates one, and its
test checks it against the settings class. [The connector guide](../docs/connector-guide.md) shows how.

### A sink

A sink implements `open()`, `backup()`, `upsert(batch, *, dry_run=False)` and `close()` (see
`catalog_sync.sinks.base`), knows nothing about what a row means, and registers under
`catalog_sync.sinks`. The same contract suite that checks the built-in sinks is shipped for yours:

```python
import pytest

from catalog_sync.testing.sink_contract import CONTRACT_CHECKS, SinkHarness, shape_of


def make_harness(workdir):  # build your sink in `workdir`, and say how to read its rows back
    sink = MySink(workdir / "store")
    # shape=shape_of(schema) runs the same checks on every column of one of your entity schemas
    return SinkHarness(sink, read_rows=..., read_backup=...)


@pytest.mark.parametrize("check", CONTRACT_CHECKS, ids=lambda check: check.__name__)
def test_my_sink_honours_the_contract(check, tmp_path):
    check(make_harness, tmp_path)
```

The contract: only owned fields are written; `keep_existing_if_null` keeps a stored value; rows are
never deleted or replaced wholesale; `backup()` precedes every load and holds the pre-load rows;
a dry run reports what a load would do and writes nothing.

A sink that stores nothing itself (the HTTP sink) cannot keep a backup or predict a dry run, and says so
instead of pretending: `backup_supported = False` and `dry_run_predicts_counts = False`. Its backup receipt
carries a note saying whose job recovery is, a dry run reports no counts and explains itself in the report's
notes, and the contract suite checks that declaration where it checks the other sinks on rows.

## Responsible use

- Export only data that you are entitled to access, and keep it for the purpose you are entitled to use it for.
- Read the terms of service of the service you pull from, and record when you did. Respect its rate limits:
  the declared ceiling is a ceiling, not a target.
- Fill in the compliance block truthfully. The tool cannot tell if you did.
- The tool does not evade anything: no anti-detection flags, no user-agent or IP rotation, back off on 429,
  no CAPTCHA solving. A login that needs a person is done by a person.
- Requests say who sends them. The default `User-Agent` is `catalog-sync/<version> (+project URL)`: the
  tool and its version, never a person or an account. A connector may send a browser's instead only as
  an exception that [docs/compliance.md](../docs/compliance.md#user-agent-policy) lists with its reason (a
  connector package lists its own in its README). The browser used by `login --browser` keeps its own user-agent.
- Redirects are not followed blindly. The HTTP client never lets the library follow one, and a POST is
  never re-sent (a login form must not carry its password to wherever a response points). A picture
  download follows at most 5 redirects, by hand: each hop is checked against the connector's URL rule,
  counts against the request ceiling, is never downgraded from https to http, and drops `Authorization`
  and `Cookie` once it is on another origin. The `https_only()` rule refuses `localhost` and private,
  loopback and link-local addresses unless a connector allows them on purpose.
- The cache holds third-party data, often copyrighted. It lives in your workspace, outside this repository,
  and must never be committed or published. Set a retention and run `cache purge`.
- Credentials come from environment variables or `0600` files in the workspace. They are never written to
  logs, and the tool does not parse `.env` files.
- This project is independent. It is not affiliated with, endorsed by or supported by any vendor whose
  service a connector talks to; their product names are trademarks of their owners.

## Security

Credentials never reach logs, error lines, snapshots or the audit log: the code is written not to put them
there, a masking layer catches what slips through, and a test pushes a canary token through every path.
Report vulnerabilities privately; see [SECURITY.md](../SECURITY.md). Before this repository is made public its
whole history is scanned for confidential terms ([docs/publishing.md](../docs/publishing.md)).

## Development

```console
$ uv sync
$ uv run ruff check . && uv run ruff format --check .
$ uv run mypy --strict src
$ uv run python tools/check_nesting.py src tools
$ uv run pytest -q
```

Tests run with sockets disabled and only synthetic data; the ones that start a Mock SaaS may reach
`127.0.0.1` and nothing else. The CI workflow is configured to run them on Python 3.11, 3.12 and 3.13, type-checks with
`mypy --strict`, runs the Postgres sink against a `postgres:16` service container, and runs the
commands of the demos above exactly as written. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the rules, including the one that every new check needs a
test that makes it fail.

A clean-install acceptance run (the package is built, installed into a fresh environment, and the installed program is driven through resume, re-run, partial failure, sensitive values and Postgres) is described in [docs/acceptance.md](../docs/acceptance.md), with a recorded run and the scope it covers.

On macOS, Python 3.13+ silently ignores `.pth` files that carry the "hidden" file flag, and some desktop
tooling sets that flag on everything inside a dot-directory such as `.venv`. If `catalog-sync` then fails with
`ModuleNotFoundError: catalog_sync`, run `chflags -R nohidden .venv`, or install a regular copy with `uv sync
--no-editable`. The test suite is unaffected: pytest puts `src` on the path itself.

## License

MIT, see [`LICENSE`](../LICENSE).
