# Acceptance: does the built package work when nobody has the source tree?

The unit and end-to-end tests run the code in this checkout. They cannot notice what only a
**clean install** shows: a file missing from the wheel, a mapping that the sdist forgot, an entry
point that points at the source tree, a command that only works from the repository root. The
acceptance harness, `tools/acceptance.py`, closes that gap. It builds the wheel and the sdist,
installs each into a fresh virtual environment, and runs the scenarios below against the
**installed** program, a Mock source and the reference platform, all on loopback.

It writes down what held and what did not. A scenario that did not run is listed as `NOT_RUN`
with its reason; it is never counted as a pass.

1. [What it proves, and what it does not](#what-it-proves-and-what-it-does-not)
2. [Reproduce it](#reproduce-it)
3. [How the inputs are generated](#how-the-inputs-are-generated)
4. [Expected row counts](#expected-row-counts)
5. [The scenarios](#the-scenarios)
6. [How the checks work](#how-the-checks-work)
7. [Supported scope](#supported-scope)
8. [Last recorded run](#last-recorded-run)

## What it proves, and what it does not

**Proves**, for the Mock SaaS and both of its entities (materials and suppliers):

- the wheel, and a wheel built from the sdist, install into an empty environment and report the
  version of the source;
- the program the commands run is the installed one: `catalog_sync.__file__` is inside the
  environment's `site-packages`, not in the source tree, and `PYTHONPATH` is not set;
- the pipeline (login, pull, normalize, load) works from that install, with the mapping each
  connector ships inside its own package and no `mapping` key in the config;
- the scenarios of the table below hold;
- the release package holds what it should and nothing else: the release checks `dist-check`,
  `licenses` and `image` (below) look inside the built wheel and sdist, at the licences of the
  installed distributions, and inside a container image built from the committed HEAD.

**Does not prove**: anything about a real service (the Mock SaaS is invented data served by this
repository), anything on an operating system or Python version that was not run (see
[Supported scope](#supported-scope)), or that a source's compliance declaration is honest (the tool
checks that it is there and consistent; it cannot check the truth of it).

The harness makes no request except to `127.0.0.1`. `uv` may fetch packages from an index while
it builds and installs.

## Reproduce it

You need `git`, [`uv`](https://docs.astral.sh/uv/) and a network for the package index. In a
checkout of this repository:

```console
$ uv sync
$ export PYTHON_DOTENV_DISABLED=1
$ uv run python tools/acceptance.py --out DIR
```

`DIR` must not exist or must be empty (`--force` removes a `DIR` that an earlier run made).
Nothing is written outside it. The exit code is `0` when nothing failed (a `NOT_RUN` does not make
it non-zero, and is listed) and `1` when any step or scenario failed.

Options:

| Option | Meaning |
|---|---|
| `--only S2 S3 ...` | Run only these scenarios or release checks (`dist-check`, `licenses`, `image`). The others are listed as `NOT_RUN`, reason "not selected". The build and install steps always run. |
| `--python 3.12` | The Python of the fresh environments. Default: the minor version of the interpreter that runs the harness. |
| `--postgres-dsn DSN` | A throw-away Postgres to load into as well. Default: `$ACCEPTANCE_POSTGRES_DSN`. The value is never printed or recorded. |
| `--force` | Remove `DIR` first, if this tool created it. |
| `--private-terms FILE` | The maintainer's private terms file ([publishing.md](publishing.md), step 1): `dist-check` looks for those terms in the wheel and the sdist and in the image's file names too. Default: `$CATALOG_SYNC_PRIVATE_TERMS_FILE`. Neither the path nor a term is recorded. |

With a disposable Postgres (bound to loopback, removed afterwards; the password is made up for the
occasion and kept out of the command line):

```console
$ export PGPASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
$ docker run -d --rm --name acceptance-pg -e POSTGRES_PASSWORD="$PGPASSWORD" -p 127.0.0.1:55432:5432 postgres:16
$ export ACCEPTANCE_POSTGRES_DSN="postgresql://postgres:${PGPASSWORD}@127.0.0.1:55432/postgres"
$ uv run python tools/acceptance.py --out DIR
$ docker rm -f acceptance-pg
```

Giving a DSN installs the `[postgres]` extra into the fresh environment and turns the `PG` scenario
on; without one `PG` is listed as `NOT_RUN` ("no DSN"). The harness makes a schema of its own in that
database (`acc_<hex>`) and drops it afterwards, so a database used for something else is not touched.

The same run as a test (slow; off unless asked for):

```console
$ CATALOG_SYNC_RUN_ACCEPTANCE=1 uv run pytest tests/test_acceptance_tool.py -m slow
```

What is in `DIR` afterwards:

| Path | Holds |
|---|---|
| `acceptance-report.md`, `acceptance-report.json` | The record: provenance, every step and scenario with its checks and evidence. Host paths are shown as `<out>`, `<repo>` and `<home>`. |
| `dist/`, `dist-from-sdist/` | The wheel and sdist, and the wheel built from the sdist. |
| `venv/`, `venv-sdist/` | The two fresh environments. |
| `work/<scenario>/` | The empty directory each scenario runs its commands in, with its `catalog-sync.toml`. |
| `home/<scenario>/` | That scenario's workspace (`CATALOG_SYNC_HOME`): state, cache, credentials, `out/`. |
| `logs/` | Every command's exit code, stdout and stderr, and the servers' access logs. |

## How the inputs are generated

Nothing is recorded from a real service. Each scenario starts its own pair of servers from the
installed program:

```console
$ catalog-sync mock serve --port 0 --verbose --seed 1 --materials 24 --suppliers 9 --pid-file ...
$ catalog-sync mock platform serve --port 0 --verbose --db ... --token-env ACCEPTANCE_PLATFORM_TOKEN --pid-file ...
```

- **The catalogue** is the Mock SaaS with seed 1: 24 materials in a folder tree (each with a
  detail record and two pictures) and 9 suppliers (next-page-token pagination, no detail, no
  pictures). The same seed gives the same data, which is what makes the expected values below
  computable. `--verbose` makes the source print one line per request (method, path, status; never a
  query, header or body): that log is where the request counts come from. Scenarios that need a
  particular behaviour add a flag: `--gone-every 12` for S5 (the detail of every 12th material
  answers 404 for good), `--materials 23` for S6 (the source loses its last item), `--token-ttl`
  and `--expire-after` for S8 (a token that is dead before the pull, and one that dies during it).
- **Credentials** are made up for each run (`secrets.token_hex`): the Mock account's user name and
  password, a wrong password, and the platform's bearer token. They reach the servers and the
  commands through environment variables only, are never written to the config, and are masked out of
  the report if they ever appeared in it.
- **The config** is written into the scenario's empty directory. Each source (`mock.materials`,
  `mock.suppliers`) has a compliance block (own account, 3000 requests per minute, review date
  today); sinks are SQLite at `{workspace}/out/mock.db` (one table per entity), the HTTP sink
  pointed at the reference platform, and a JSONL file; a profile per entity ties them together. No
  `mapping` key appears anywhere, so the packaged mappings apply.
- **The environment** of every command under test: `CATALOG_SYNC_HOME` under `DIR/home`, `TMPDIR`
  under `DIR`, `PYTHON_DOTENV_DISABLED=1`, no `PYTHONPATH`, and a `HOME` that is a throw-away
  directory under `DIR`, so that a command which forgot the workspace could not write into yours.

## Expected row counts

They come from the seeded catalogue (`tests/fixtures/mock_saas.py` is the oracle), not from numbers
typed into the harness.

| Entity | Rows (SQLite, platform, JSONL) | Listing requests | Detail requests, first run | Picture requests, first run |
|---|---|---|---|---|
| materials | 24 | 6 | 24 | 48 |
| suppliers | 9 | 3 | 0 | 0 |

## The scenarios

Each runs for both entities unless noted, into SQLite and the reference platform (a JSONL sink
too, where the field comparison needs it).

| ID | Scenario | What must hold |
|---|---|---|
| S1 | Resume | Materials. A `pull` is SIGKILLed once the source has seen 9 detail requests *from it*; the trigger is that observed count, never a sleep. A `pull --resume` is then SIGKILLed after 15 picture requests, and a last `pull --resume` runs to the end. Across the three processes no detail and no picture is requested twice, except the one request that was in flight at a kill; every item gets its detail once and both pictures; the resumed pulls make no listing request; the run ends `complete`, with all 24 items done at every stage. Normalize and load then give 24 rows in SQLite, the platform and the JSONL sink, field-for-field equal to the oracle. |
| S2 | Re-run | Run the profile twice. The second run makes **zero** detail and picture requests, still walks the listing, and loads `inserted 0, updated 0`. SQLite, platform and JSONL row counts equal the catalogue's after both runs. Sending the identical normalized file to the platform again is **replayed** under the same idempotency key and adds no row. Every owned field agrees across the normalized file, the JSONL sink, SQLite and the platform; a fixed sample of ids matches the oracle's expected record in all four. |
| S3 | Offline re-normalize | Stop the Mock source; confirm its port is closed. Change one rule of a copy of the packaged mapping (materials: the price currency `USD` to `EUR`; suppliers: `country` read from `name`). `normalize --mapping` on the cached run succeeds, and every record differs from the baseline exactly as the change predicts. The sidecar carries a new `mapping_sha256` for the same run. |
| S4 | Partial upsert | After a first `run`, the SQLite table gets a column `ops_note` with a value per row and a child table with two foreign-key rows per record (`ON DELETE CASCADE`); the platform's table gets an `ops_note` column too. `pull --full` then makes every row differ, and loading it into SQLite and the platform reports every row `updated` (so the load really wrote). Afterwards the `ops_note` values are unchanged in both, every child row is there with no broken reference, and the owned fields hold the new load's values. |
| S5 | Partial failure | Materials. With `--gone-every 12` the details of `mat-0012` and `mat-0024` answer 404 for good (`--error-every` answers 503, which the client retries, so it cannot make an item fail). `run` exits **5**: 2 of 24 fetched items failed (8.3%), within the 10% default limit. `runs show` lists exactly those two, with stage and reason; the source refused each detail once and no picture of them was fetched; SQLite, the platform and the JSONL sink hold exactly the other 22, equal to the oracle. In a second fresh workspace the same run with `--max-failed-ratio 0` exits **1**, and still loads the 22. |
| S6 | Vanished source record | Materials. After run 1 (24 rows) the source is restarted on the same port with `--materials 23`: the first 23 of the same seeded catalogue, so `mat-0024` is gone. Run 2 exits **0** (no item failed; a failed item would suppress the report). The SQLite and platform loads say `1 stored row(s) no longer in the source (reported, kept)`; `mat-0024` is still in both, unchanged, and the audit log's load entries record `absent` 1. |
| S7 | Sensitive values | Which fields are sensitive is read from the installed mappings (`sensitive: true`: the suppliers' `contact_email`); the values come from the normalized records, each confirmed against the seeded catalogue. Looked for: those values, the account password, a wrong password, the user name, the platform token, a wrong platform token and the access token the login stored. Searched: every command's stdout and stderr (a refused login and a refused load included), the workspace (state, `audit.jsonl`, `out/`, credentials, cache), the platform's database, the work directory and the server logs. The sensitive values occur only in the raw cache and in the normalized JSONL (documented in [data-contract.md](data-contract.md) section 9; all 9 are found there, which proves the search works); the access token only in the credential store; the user name only in the Mock source's own start-up line; everything else nowhere. SQLite and the platform have no `contact_email` column. |
| S8 | Refusals | A config with no compliance block: `login`, `pull`, `inspect` and `run` exit **4**, and the source saw **zero** requests. A wrong password: `login` exits **2** after exactly one request (the login, refused with 401); a later `pull` exits 2 and sends nothing. A token that is already expired when the pull starts (`--token-ttl 2`, then a wait): exit **2**, zero requests, and the message says how to log in again. A token that dies **during** the pull (`--expire-after 2`: the source answers 401 to the request after the second since the login, whatever the clock says, so no pace has to be tuned): exit **2**, exactly one 401, as the last request, with no retry. |
| S9 | Real workspace | The default workspace (`~/.local/share/catalog-sync`, and `$XDG_DATA_HOME/catalog-sync` if that variable is set) is fingerprinted before the first command and after the last scenario (path, size and modification time; no file is opened) and must be unchanged. No command may have written a default workspace under the throw-away `HOME` either. |
| PG | Postgres | With a DSN: both profiles also load into a Postgres schema of the harness's own. Run 1 inserts every row (24 and 9), run 2 changes none; Postgres and SQLite hold the same rows; every owned field agrees across the normalized JSONL, SQLite and Postgres; a fixed sample matches the oracle; the sensitive field has no column. Without a DSN: `NOT_RUN`, "no DSN". |

### Release checks

They run after the scenarios, on what the steps built, and are registered the same way
(`tools/acceptance_release.py`). What they fail on, and the list of files that must never ship, is in
[publishing.md](publishing.md), step 9.

| ID | Check | What must hold |
|---|---|---|
| dist-check | Built wheel and sdist | `tools/check_dist.py` finds nothing: no forbidden term (private ones too, when a terms file is given) or public pattern in a name or a file; no `/Users/<name>` or `/home/<name>` path; no entry point or package the root `pyproject.toml` does not register; none of the files that must never ship; `LICENSE`, `README.md` (sdist) and the licence metadata (wheel) present. One row of checks per rule, and the number of entries read in each archive. |
| licenses | Installed distributions | The licence of every distribution in the clean wheel environment (the `postgres` extra when a DSN was given) is stated and is not copyleft, except the reviewed exceptions. The table of all of them is in the report. |
| image | Container image | The `Dockerfile` is built from `git archive HEAD` with a unique tag and run with no network: `catalog-sync` is the only distribution of that name, only root-registered entry points exist, the user is not root, no file under `/app` is on the never-ship list, and `--version` is the source's. Size, user and the distributions are recorded. The tag is removed afterwards. `NOT_RUN` when Docker is not there. |

## How the checks work

- **Request counting.** The source's `--verbose` lines are parsed into method, path and status, and
  classified as login, listing, detail, picture or other. A scenario takes a mark in the log before a
  command and reads the requests after it. In S2 the counter first has to see run 1's requests (24
  details and 48 pictures for materials); only then does run 2's zero mean anything.
- **Field comparison.** The fields compared are the ones the normalized file's sidecar lists as
  `owned`, minus those a sink's drop policy withholds (`contact_email`). For each loaded record the
  value must be equal, as canonical JSON, in each store. The report states the number of records, the
  number of field values compared, and every mismatch (the first twenty in full).
- **Interrupting a command (S1).** The command starts in the background, in a process group of its
  own. The harness polls the source's request log and sends SIGKILL when the source has seen the
  wanted number of requests of the wanted kind; a command that ended first is reported as a problem
  of the harness, never as a pass. After the kill it waits until the log stops growing, so that a
  request the dying client had sent is not missed.
- **Looking for values (S7).** Each value is searched as UTF-8 text in the bytes of every file and of
  every command's output; a SQLite file keeps its text as it is, so a value in a row is found. A kind
  of value may be found only in the places the contract names, and the search first has to find the
  sensitive values where the contract says they are, or it proves nothing. The harness's own
  transcripts mask the throw-away credentials, so they cannot show a credential leak; the commands'
  raw output, taken before masking, is what is searched.
- **Provenance.** The report starts with the version, the commit, whether the tree was dirty, the
  Python versions, the OS, the `uv` version, the UTC time and the harness command line.

## Supported scope

What the package is built and tested for, so that a record of a run can be read against it.

- **Python.** `requires-python >= 3.11`. What the harness actually ran: Python 3.13, both as the
  interpreter of the harness and of the fresh environments, and only that. The CI matrix is
  configured for 3.11 to 3.13 (the test job; the acceptance job uses 3.12), but it has not run,
  because this repository has no remote yet.
- **Operating systems run through this harness.** macOS on Apple silicon only. No other OS has been
  run through it.
- **Entities.** `material` and `supplier`, both with the built-in schemas; a connector can declare
  its own schema.
- **Sinks.** `jsonl`, `csv`, `sqlite`, `postgres` (needs the `postgres` extra) and `http` (your
  platform, [platform-contract.md](platform-contract.md)). The acceptance scenarios use `sqlite`, `http`
  and `jsonl`, and `postgres` when a DSN is given; `csv` is not exercised by the harness.
- **Authentication kinds** the SDK and the scaffold support: a bearer token, a session cookie, an API
  key header, and a login request that answers with a token (`form`). A credential is obtained by pasting a
  copied cURL command, by a plain HTTP login, or through a browser (the `browser` extra) that a person
  can drive.
- **Pagination kinds** the SDK and the scaffold support: page number with a total (`page`), next-page
  token (`token`), and offset with limit (`offset`).
- **Connectors in the release package.** The Mock SaaS only (`mock.materials`, `mock.suppliers`), plus
  `replay`, which reads a stored snapshot and makes no request. Connectors for real services live outside
  this package.
- **Container stack.** The compose stack of [deploy.md](deploy.md) (materials only) was run locally,
  from a clean export of the repository at commit `d326a79`: 24 rows reached Postgres and 24 the
  platform. The image has no connector distributions in it. This is a recorded fact about that one run;
  the acceptance harness does not repeat it.
- **Not supported.** Requests signed by JavaScript on the page; sites with no JSON behind them
  (HTML scraping); CAPTCHAs and second factors (a person does them; the tool never bypasses them);
  and "any website": a source needs a connector, and a connector needs an account you are allowed
  to use and JSON endpoints to call ([connector-guide.md](connector-guide.md), "What this cannot do").

## Last recorded run

The report below is `acceptance-report.md` of one run of `tools/acceptance.py` on commit
`500e66ecfe110bb841062723736d8dc67d3ab574` (a clean tree, version `0.1.0rc1`), with a disposable `postgres:16` container bound to
loopback and a private terms file of 6 throw-away entries, which `dist-check` looked for in the
wheel, the sdist and the image's file names. The file is not in the repository, and neither its path
nor a term is in the report. This record was produced before the framework-only public export. The original private
source then received a documentation-only update and was independently rechecked at `86d3f51`.
The public candidate starts a separate history, excludes real-service connectors, and updates
introductory copy and CI handling of an empty connector directory. The commit identifiers in
this historical record belong to the original repository and are not expected to resolve here.
This record is not a claim that GitHub CI has run on the public candidate. Host paths are the
placeholders `<out>`, `<repo>` and `<home>`; the throw-away credentials and the DSN are not in it.
The JSON report next to it has the same content.

### Acceptance record

**Result: PASS** (18 PASS, 0 FAIL, 0 NOT_RUN)

A NOT_RUN is a check that did not happen. It is listed with its reason and is never a pass.

#### Provenance

- version: `0.1.0rc1`
- commit: `500e66ecfe110bb841062723736d8dc67d3ab574`
- dirty: `False`
- python (harness): `3.13.17`
- python (fresh environments): `3.13`
- os: `Darwin 25.5.0 (arm64)`
- uv: `uv 0.13.5 (Homebrew 2026-10-22 aarch64-apple-darwin)`
- time (UTC): `2026-10-27T22:05:35Z`
- postgres: `a DSN was given (its value is not recorded)`
- private terms: `a terms file was given (neither its path nor a term is recorded)`
- command: `uv run python tools/acceptance.py --out <out> --postgres-dsn <dsn> --private-terms <file>`

#### Steps

| ID | What | Status | Reason |
|---|---|---|---|
| build | Build the wheel and the sdist | PASS |  |
| install-wheel | Install the wheel into a fresh environment | PASS |  |
| install-sdist | Build a wheel from the sdist and install it into a second environment | PASS |  |
| installed-copy | The installed copy is the one under test | PASS |  |
| servers | Start and stop the installed Mock source and reference platform | PASS |  |

#### Scenarios

| ID | What | Status | Reason |
|---|---|---|---|
| S1 | Resume after a killed pull: finished work is not fetched again | PASS |  |
| S2 | Re-run: no refetch, no duplicate rows, replayed keys, equal fields | PASS |  |
| S3 | Offline re-normalize with a changed mapping | PASS |  |
| S4 | Partial upsert keeps a platform-owned column and a child table | PASS |  |
| S5 | Partial failure: exit 5, failed items listed, the rest loaded | PASS |  |
| S6 | A vanished source record is reported and kept | PASS |  |
| S7 | Sensitive values never leave the places they may be | PASS |  |
| S8 | Refusals: no declaration, wrong password, expired token | PASS |  |
| PG | Postgres: the same profiles load into a real Postgres | PASS |  |
| S9 | The real default workspace is untouched | PASS |  |
| dist-check | The built wheel and sdist hold nothing that must not ship | PASS |  |
| licenses | The licence of every installed distribution is known and not copyleft | PASS |  |
| image | The container image holds the core package only and runs as non-root | PASS |  |

#### Details

##### build: Build the wheel and the sdist: PASS

| Check | Result | Observed |
|---|---|---|
| uv build exits 0 | pass | exit 0: Building source distribution... Building wheel from source distribution... Successfully built <out>/dist/catalog_sync-0.1.0rc1.tar.gz Successfully built <out>/dist/catalog_sync-0.1.0rc1-py3-none-any.whl |
| one wheel and one sdist were built | pass |  |

- wheel: `{"bytes": 323995, "file": "catalog_sync-0.1.0rc1-py3-none-any.whl", "sha256": "20f823d2665a1a9a959af407833b3b67e5abe97974c23fd3a4b9abc492b0aac9"}`
- sdist: `{"bytes": 781674, "file": "catalog_sync-0.1.0rc1.tar.gz", "sha256": "64d4ab7c119a0dae81915371a11a2234044d1556418e4a3a1f086c219ca65bdf"}`

##### install-wheel: Install the wheel into a fresh environment: PASS

| Check | Result | Observed |
|---|---|---|
| wheel: `catalog-sync --version` is the version of the source | pass | catalog-sync 0.1.0rc1 |


##### install-sdist: Build a wheel from the sdist and install it into a second environment: PASS

| Check | Result | Observed |
|---|---|---|
| a wheel builds from the sdist | pass | exit 0: Building wheel from source distribution... Successfully built dist-from-sdist/catalog_sync-0.1.0rc1-py3-none-any.whl |
| sdist: `catalog-sync --version` is the version of the source | pass | catalog-sync 0.1.0rc1 |

- wheel built from the sdist: `{"file": "catalog_sync-0.1.0rc1-py3-none-any.whl", "sha256": "20f823d2665a1a9a959af407833b3b67e5abe97974c23fd3a4b9abc492b0aac9"}`
- same bytes as the wheel built from the checkout: `true`

##### installed-copy: The installed copy is the one under test: PASS

| Check | Result | Observed |
|---|---|---|
| wheel: catalog_sync is imported from the environment's site-packages | pass | <out>/venv/lib/python3.13/site-packages/catalog_sync/__init__.py |
| sdist: catalog_sync is imported from the environment's site-packages | pass | <out>/venv-sdist/lib/python3.13/site-packages/catalog_sync/__init__.py |

- wheel: catalog_sync.__file__: `"<out>/venv/lib/python3.13/site-packages/catalog_sync/__init__.py"`
- sdist: catalog_sync.__file__: `"<out>/venv-sdist/lib/python3.13/site-packages/catalog_sync/__init__.py"`
- PYTHONPATH in the commands' environment: `"absent"`

##### servers: Start and stop the installed Mock source and reference platform: PASS

| Check | Result | Observed |
|---|---|---|
| the Mock source listens on loopback | pass |  |
| the reference platform listens on loopback | pass |  |
| the config has no mapping key | pass |  |
| the installed program reads that config (runs list exits 0) | pass |  |
| no request reached the source meanwhile | pass |  |
| both servers exit 0 on SIGTERM | pass |  |
| their ports are closed afterwards | pass |  |


##### S1: Resume after a killed pull: finished work is not fetched again: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| the pull was killed (SIGKILL) after 9 detail request(s) it was seen to make | pass | exit -9, {'list': 6, 'detail': 9} |
| the workspace holds exactly one run | pass | 1 run(s) |
| the killed run is left unfinished (status running), mid-way through the detail stage | pass | {'list': 24, 'detail': 9} |
| the pull was killed (SIGKILL) after 15 asset request(s) it was seen to make | pass | exit -9, {'detail': 15, 'asset': 15} |
| the last `pull --resume` exits 0 | pass |  |
| no detail request was made twice, except one that was in flight at a kill | pass | 0 repeated of 24 distinct |
| every one of the 24 items got its 1 detail request(s), none missed | pass | 24 items |
| no asset request was made twice, except one that was in flight at a kill | pass | 0 repeated of 48 distinct |
| every one of the 24 items got its 2 asset request(s), none missed | pass | 24 items |
| the resumed pulls did not walk the listing again (it was finished before the first kill) | pass | listing requests per phase: [6, 0, 0] |
| the first kill fell in the detail stage (no picture was fetched yet), the second in the asset stage (every detail was done) | pass |  |
| the resumed run is complete: status complete, 24 items, every stage done | pass | {'list': 24, 'detail': 24, 'assets': 24} |
| materials: normalize exits 0 | pass |  |
| materials: load --sink sqlite-materials exits 0 | pass |  |
| materials: load --sink platform-materials exits 0 | pass |  |
| materials: load --sink jsonl-materials exits 0 | pass |  |
| materials: every owned field agrees across the normalized JSONL, the JSONL sink, SQLite and the platform | pass | 24 records, 1152 field values compared, 0 mismatch(es) |
| materials: a fixed sample of 9 ids matches the oracle in all four stores | pass | 0 mismatch(es) |
| materials: after run 1, SQLite, the platform and the JSONL sink hold the 24 seeded rows | pass | {'sqlite': 24, 'platform': 24, 'jsonl sink': 24} |

- after the first kill: stages done: `{"detail": 9, "list": 24}`
- after the second kill: stages done: `{"assets": 7, "detail": 24, "list": 24}`
- detail requests per phase (kill, kill, end): `[9, 15, 0]`
- asset requests per phase (kill, kill, end): `[0, 15, 33]`
- the final run: stages done: `{"assets": 24, "detail": 24, "list": 24}`
- materials: field comparison: `{"field_values_compared": 1152, "mismatch_count": 0, "mismatches_shown": [], "owned fields compared": ["code", "composition", "composition_raw", "extra", "fetched_at", "folder_paths", "images", "issues", "name", "price", "raw_ref", "schema_version", "source_version", "supplier", "weight_gsm", "width_cm"], "records": 24, "stores": ["normalized JSONL", "JSONL sink", "SQLite", "platform"]}`
- materials: oracle sample: `{"ids": ["mat-0001", "mat-0004", "mat-0007", "mat-0010", "mat-0013", "mat-0016", "mat-0019", "mat-0022", "mat-0024"], "mismatches": []}`
- materials: expected row count (the seeded catalogue): `24`
- materials: rows after run 1, after run 2: `[{"jsonl sink": 24, "platform": 24, "sqlite": 24}]`

##### S2: Re-run: no refetch, no duplicate rows, replayed keys, equal fields: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| materials: run 1 exits 0 | pass |  |
| materials: run 2 exits 0 | pass |  |
| materials: run 1 fetched each item's detail once and each picture once | pass | detail 24, asset 48 |
| materials: run 2 made zero detail and zero asset requests | pass | detail 0, asset 0 |
| materials: run 2 still walked the listing, as many pages as run 1 | pass | 6 vs 6 listing requests |
| materials: after run 1, SQLite, the platform and the JSONL sink hold the 24 seeded rows | pass | {'sqlite': 24, 'platform': 24, 'jsonl sink': 24} |
| materials: after run 2, SQLite, the platform and the JSONL sink hold the 24 seeded rows | pass | {'sqlite': 24, 'platform': 24, 'jsonl sink': 24} |
| materials: run 2 inserted and updated nothing in SQLite or on the platform | pass | {'platform-materials': (0, 0, 24), 'sqlite-materials': (0, 0, 24)} |
| materials: normalize of the latest run exits 0 | pass |  |
| materials: load --sink platform-materials (time 1) exits 0 | pass |  |
| materials: load --sink platform-materials (time 2) exits 0 | pass |  |
| materials: loading the same file again is a replay under the same idempotency key | pass | ['replayed'] |
| materials: the replays added no row on the platform | pass | 24 rows before, 24 after |
| materials: every owned field agrees across the normalized JSONL, the JSONL sink, SQLite and the platform | pass | 24 records, 1152 field values compared, 0 mismatch(es) |
| materials: a fixed sample of 9 ids matches the oracle in all four stores | pass | 0 mismatch(es) |
| suppliers: run 1 exits 0 | pass |  |
| suppliers: run 2 exits 0 | pass |  |
| suppliers: run 1 fetched each item's detail once and each picture once | pass | detail 0, asset 0 |
| suppliers: run 2 made zero detail and zero asset requests | pass | detail 0, asset 0 |
| suppliers: run 2 still walked the listing, as many pages as run 1 | pass | 3 vs 3 listing requests |
| suppliers: after run 1, SQLite, the platform and the JSONL sink hold the 9 seeded rows | pass | {'sqlite': 9, 'platform': 9, 'jsonl sink': 9} |
| suppliers: after run 2, SQLite, the platform and the JSONL sink hold the 9 seeded rows | pass | {'sqlite': 9, 'platform': 9, 'jsonl sink': 9} |
| suppliers: run 2 inserted and updated nothing in SQLite or on the platform | pass | {'platform-suppliers': (0, 0, 9), 'sqlite-suppliers': (0, 0, 9)} |
| suppliers: normalize of the latest run exits 0 | pass |  |
| suppliers: load --sink platform-suppliers (time 1) exits 0 | pass |  |
| suppliers: load --sink platform-suppliers (time 2) exits 0 | pass |  |
| suppliers: loading the same file again is a replay under the same idempotency key | pass | ['replayed'] |
| suppliers: the replays added no row on the platform | pass | 9 rows before, 9 after |
| suppliers: every owned field agrees across the normalized JSONL, the JSONL sink, SQLite and the platform | pass | 9 records, 324 field values compared, 0 mismatch(es) |
| suppliers: a fixed sample of 4 ids matches the oracle in all four stores | pass | 0 mismatch(es) |

- materials: idempotency keys the platform holds after run 1: `1`
- materials: idempotency keys the platform holds after run 2: `2`
- materials: requests in run 1: `{"asset": 48, "detail": 24, "list": 6}`
- materials: requests in run 2: `{"list": 6}`
- materials: expected row count (the seeded catalogue): `24`
- materials: rows after run 1, after run 2: `[{"jsonl sink": 24, "platform": 24, "sqlite": 24}, {"jsonl sink": 24, "platform": 24, "sqlite": 24}]`
- materials: run 2 load counts (inserted, updated, unchanged): `{"jsonl-materials": [0, 0, 24], "platform-materials": [0, 0, 24], "sqlite-materials": [0, 0, 24]}`
- materials: platform answers to the same file, loaded twice: `{"first": [["replayed", "eadd9e1c2084"]], "second": [["replayed", "eadd9e1c2084"]], "stored keys after each": [2, 2]}`
- materials: field comparison: `{"field_values_compared": 1152, "mismatch_count": 0, "mismatches_shown": [], "owned fields compared": ["code", "composition", "composition_raw", "extra", "fetched_at", "folder_paths", "images", "issues", "name", "price", "raw_ref", "schema_version", "source_version", "supplier", "weight_gsm", "width_cm"], "records": 24, "stores": ["normalized JSONL", "JSONL sink", "SQLite", "platform"]}`
- materials: oracle sample: `{"ids": ["mat-0001", "mat-0004", "mat-0007", "mat-0010", "mat-0013", "mat-0016", "mat-0019", "mat-0022", "mat-0024"], "mismatches": []}`
- suppliers: idempotency keys the platform holds after run 1: `3`
- suppliers: idempotency keys the platform holds after run 2: `4`
- suppliers: requests in run 1: `{"list": 3}`
- suppliers: requests in run 2: `{"list": 3}`
- suppliers: expected row count (the seeded catalogue): `9`
- suppliers: rows after run 1, after run 2: `[{"jsonl sink": 9, "platform": 9, "sqlite": 9}, {"jsonl sink": 9, "platform": 9, "sqlite": 9}]`
- suppliers: run 2 load counts (inserted, updated, unchanged): `{"jsonl-suppliers": [0, 0, 9], "platform-suppliers": [0, 0, 9], "sqlite-suppliers": [0, 0, 9]}`
- suppliers: platform answers to the same file, loaded twice: `{"first": [["replayed", "c1aaf4d9afe1"]], "second": [["replayed", "c1aaf4d9afe1"]], "stored keys after each": [4, 4]}`
- suppliers: field comparison: `{"field_values_compared": 324, "mismatch_count": 0, "mismatches_shown": [], "owned fields compared": ["address", "certifications", "country", "fetched_at", "issues", "lead_time_days", "listed_at", "name", "rating", "raw_ref", "schema_version", "source_version"], "records": 9, "stores": ["normalized JSONL", "JSONL sink", "SQLite", "platform"]}`
- suppliers: oracle sample: `{"ids": ["sup-001", "sup-004", "sup-007", "sup-009"], "mismatches": []}`

##### S3: Offline re-normalize with a changed mapping: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| materials: pull exits 0 | pass |  |
| materials: normalize with the packaged mapping exits 0 | pass |  |
| suppliers: pull exits 0 | pass |  |
| suppliers: normalize with the packaged mapping exits 0 | pass |  |
| the Mock source is stopped and its port is closed | pass |  |
| materials: the change touches exactly one rule of the packaged mapping | pass | 'currency: USD' -> 'currency: EUR' |
| materials: normalize --mapping exits 0 with the source stopped | pass |  |
| materials: the output differs from the baseline exactly as the change predicts | pass | 24 of 24 records changed |
| materials: the sidecar records the new mapping on the same run | pass |  |
| suppliers: the change touches exactly one rule of the packaged mapping | pass | 'from: [list.country]' -> 'from: [list.name]' |
| suppliers: normalize --mapping exits 0 with the source stopped | pass |  |
| suppliers: the output differs from the baseline exactly as the change predicts | pass | 9 of 9 records changed |
| suppliers: the sidecar records the new mapping on the same run | pass |  |

- materials: records that the change altered: `24`
- suppliers: records that the change altered: `9`

##### S4: Partial upsert keeps a platform-owned column and a child table: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| materials: the first run exits 0 | pass |  |
| materials: pull --full exits 0 | pass |  |
| materials: normalize exits 0 | pass |  |
| materials: the sqlite load rewrote the owned fields of every row (all updated) | pass | exit 0, (0, 24, 0) |
| materials: the platform load rewrote the owned fields of every row (all updated) | pass | exit 0, (0, 24, 0) |
| materials: the SQLite column ops_note, added by the target, holds the values it had before the load | pass | 24 rows |
| materials: the load added no column of its own to the SQLite table beyond its owned fields | pass |  |
| materials: the platform column ops_note, added by the target, holds the values it had before the load | pass | 24 rows |
| materials: the load added no column of its own to the platform table beyond its owned fields | pass |  |
| materials: the child table's 48 foreign-key rows all survive, unchanged | pass | 48 rows after, 0 broken reference(s) |
| materials: the owned fields now hold the new load's values in SQLite and on the platform, next to the extras | pass | 768 field values compared, 0 mismatch(es) |
| suppliers: the first run exits 0 | pass |  |
| suppliers: pull --full exits 0 | pass |  |
| suppliers: normalize exits 0 | pass |  |
| suppliers: the sqlite load rewrote the owned fields of every row (all updated) | pass | exit 0, (0, 9, 0) |
| suppliers: the platform load rewrote the owned fields of every row (all updated) | pass | exit 0, (0, 9, 0) |
| suppliers: the SQLite column ops_note, added by the target, holds the values it had before the load | pass | 9 rows |
| suppliers: the load added no column of its own to the SQLite table beyond its owned fields | pass |  |
| suppliers: the platform column ops_note, added by the target, holds the values it had before the load | pass | 9 rows |
| suppliers: the load added no column of its own to the platform table beyond its owned fields | pass |  |
| suppliers: the child table's 18 foreign-key rows all survive, unchanged | pass | 18 rows after, 0 broken reference(s) |
| suppliers: the owned fields now hold the new load's values in SQLite and on the platform, next to the extras | pass | 216 field values compared, 0 mismatch(es) |

- materials: sqlite load (inserted, updated, unchanged): `[0, 24, 0]`
- materials: platform load (inserted, updated, unchanged): `[0, 24, 0]`
- suppliers: sqlite load (inserted, updated, unchanged): `[0, 9, 0]`
- suppliers: platform load (inserted, updated, unchanged): `[0, 9, 0]`

##### S5: Partial failure: exit 5, failed items listed, the rest loaded: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| the run exits 5: it finished, with failed items | pass | exit 5 |
| the failed share is within the limit (max_failed_ratio, default) and says so | pass | 2 of 24 fetched item(s) failed (8.3%), within the limit of 10.0% |
| `runs show` lists exactly the failed items, each with its stage and its reason | pass | {'mat-0012': 'detail request answered HTTP 404', 'mat-0024': 'detail request answered HTTP 404'} |
| the source refused each failed item's detail once, and no picture of it was fetched | pass | {'mat-0012': 1, 'mat-0024': 1} |
| normalize of the failed run exits 0 | pass |  |
| every store holds exactly the 22 items that did not fail (24 - 2), and none of the failed ones | pass |  |
| materials: every owned field agrees across the normalized JSONL, the JSONL sink, SQLite and the platform | pass | 22 records, 1056 field values compared, 0 mismatch(es) |
| materials: a fixed sample of 8 ids matches the oracle in all four stores | pass | 0 mismatch(es) |
| login --form exits 0 | pass |  |
| the same run with --max-failed-ratio 0 exits 1: the same failed items are over the limit | pass | exit 1: 2 of 24 fetched item(s) failed (8.3%), over the limit of 0.0% |
| a run that counts as failed still loaded what arrived | pass | 22 rows in SQLite |

- the items whose detail the source refuses (--gone-every): `["mat-0012", "mat-0024"]`
- rows loaded, per store: `{"JSONL sink": 22, "SQLite": 22, "normalized JSONL": 22, "platform": 22}`
- materials: field comparison: `{"field_values_compared": 1056, "mismatch_count": 0, "mismatches_shown": [], "owned fields compared": ["code", "composition", "composition_raw", "extra", "fetched_at", "folder_paths", "images", "issues", "name", "price", "raw_ref", "schema_version", "source_version", "supplier", "weight_gsm", "width_cm"], "records": 22, "stores": ["normalized JSONL", "JSONL sink", "SQLite", "platform"]}`
- materials: oracle sample: `{"ids": ["mat-0001", "mat-0004", "mat-0007", "mat-0010", "mat-0014", "mat-0017", "mat-0020", "mat-0023"], "mismatches": []}`

##### S6: A vanished source record is reported and kept: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| run 1 exits 0 | pass |  |
| run 1 stored all 24 items | pass | {'SQLite': 24, 'platform': 24} |
| login --form exits 0 | pass |  |
| run 2, with one item removed at the source, exits 0 (no item failed) | pass | exit 0:  |
| the sqlite load says 1 stored row(s) no longer in the source (reported, kept) | pass |   inserted 0, updated 0, unchanged 23   1 stored row(s) no longer in the source (reported, kept)   backup: mock.db.bak-20261027T220625115659Z (24 row(s) before) |
| the platform load says 1 stored row(s) no longer in the source (reported, kept) | pass | ource (reported, kept)   backup: none taken: the rows live on the receiving platform, whose own backups and history are the way back (docs/platform-contract.md) |
| SQLite: the row of mat-0024 is still there, as it was | pass | 24 rows |
| platform: the row of mat-0024 is still there, as it was | pass | 24 rows |
| the audit log records the vanished row of the sqlite and platform loads | pass | [1, 1, 1] |

- `absent` of the last loads in audit.jsonl: `[1, 1, 1]`

##### S7: Sensitive values never leave the places they may be: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| materials: run exits 0 | pass |  |
| suppliers: run exits 0 | pass |  |
| a login with the wrong password is refused | pass |  |
| materials: normalize exits 0 | pass |  |
| suppliers: normalize exits 0 | pass |  |
| a load with the wrong platform token is refused (non-zero exit) | pass | exit 2 |
| materials: the sidecar of the normalized file names the same sensitive fields as the packaged mapping | pass |  |
| suppliers: the sidecar of the normalized file names the same sensitive fields as the packaged mapping | pass |  |
| suppliers sup-001: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-002: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-003: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-004: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-005: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-006: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-007: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-008: contact_email is the seeded catalogue's value | pass |  |
| suppliers sup-009: contact_email is the seeded catalogue's value | pass |  |
| the packaged mappings mark at least one field sensitive, and there are values to look for | pass | 9 distinct value(s) |
| the credential store holds an access secret to look for (a positive control) | pass | 1 value(s) |
| a sensitive field's value: found only in normalized JSONL, workspace/cache | pass | found in: normalized JSONL, workspace/cache |
| a wrong password: found nowhere | pass | found in: no place |
| a wrong platform token: found nowhere | pass | found in: no place |
| the access token or session the login stored: found only in workspace/credentials | pass | found in: workspace/credentials |
| the account password: found nowhere | pass | found in: no place |
| the account user name: found only in Mock source log | pass | found in: Mock source log |
| the platform token: found nowhere | pass | found in: no place |
| documented behaviour: all 9 sensitive values are present in workspace/cache (treat the workspace and every normalized file as that sensitive) | pass | 9 of 9 |
| documented behaviour: all 9 sensitive values are present in normalized JSONL (treat the workspace and every normalized file as that sensitive) | pass | 9 of 9 |

- fields the packaged mappings mark sensitive: `{"materials": [], "suppliers": ["contact_email"]}`
- where each kind of value was found (distinct values): `{"a sensitive field's value -> normalized JSONL": 9, "a sensitive field's value -> workspace/cache": 9, "the access token or session the login stored -> workspace/credentials": 1, "the account user name -> Mock source log": 1}`

##### S8: Refusals: no declaration, wrong password, expired token: PASS

| Check | Result | Observed |
|---|---|---|
| no compliance block: `login mock --form` exits 4 | pass | exit 4 |
| no compliance block: `pull mock-materials` exits 4 | pass | exit 4 |
| no compliance block: `inspect mock-materials` exits 4 | pass | exit 4 |
| no compliance block: `run mock-materials` exits 4 | pass | exit 4 |
| no compliance block: `pull mock-suppliers` exits 4 | pass | exit 4 |
| no compliance block: `inspect mock-suppliers` exits 4 | pass | exit 4 |
| no compliance block: `run mock-suppliers` exits 4 | pass | exit 4 |
| no compliance block: zero requests reached the source | pass | 0 request(s) |
| wrong password: login exits 2 | pass | exit 2 |
| wrong password: the source saw exactly one request, the login, and refused it | pass | [('POST', '/api/login', 401)] |
| wrong password: materials pull exits 2 (no credential was stored) | pass | exit 2 |
| wrong password: suppliers pull exits 2 (no credential was stored) | pass | exit 2 |
| wrong password: no further request reached the source | pass |  |
| login --form exits 0 | pass |  |
| expired token, known before the pull: materials pull exits 2 | pass | exit 2 |
| expired token, known before the pull: materials made zero requests and says how to log in again | pass | 0 request(s) |
| expired token, known before the pull: suppliers pull exits 2 | pass | exit 2 |
| expired token, known before the pull: suppliers made zero requests and says how to log in again | pass | 0 request(s) |
| login --form exits 0 | pass |  |
| token expiring mid-run: materials pull exits 2 | pass | exit 2 |
| token expiring mid-run: materials got the requests the login had left, then exactly one 401, which was its last request: no retry | pass | [200, 401]; the login itself had used 1 of 2 |
| login --form exits 0 | pass |  |
| token expiring mid-run: suppliers pull exits 2 | pass | exit 2 |
| token expiring mid-run: suppliers got the requests the login had left, then exactly one 401, which was its last request: no retry | pass | [200, 401]; the login itself had used 1 of 2 |

- token lifetime (seconds): `2.0`
- data requests a login works for (mock serve --expire-after): `2`
- materials: statuses of the pull whose login ran out: `[200, 401]`
- suppliers: statuses of the pull whose login ran out: `[200, 401]`

##### PG: Postgres: the same profiles load into a real Postgres: PASS

| Check | Result | Observed |
|---|---|---|
| login --form exits 0 | pass |  |
| materials: run 1 exits 0 | pass |  |
| materials: run 2 exits 0 | pass |  |
| materials: run 1 inserted all 24 rows into Postgres, run 2 changed none | pass | [(24, 0, 0), (0, 0, 24)] |
| materials: Postgres and SQLite hold the same 24 rows | pass | {'normalized JSONL': 24, 'SQLite': 24, 'Postgres': 24} |
| materials: every owned field agrees across the normalized JSONL, SQLite and Postgres | pass | 24 records, 768 field values compared, 0 mismatch(es) |
| materials: a fixed sample of 9 ids matches the oracle in Postgres | pass | 0 mismatch(es) |
| materials: the sensitive fields (none) have no column in Postgres (the drop policy) | pass |  |
| suppliers: run 1 exits 0 | pass |  |
| suppliers: run 2 exits 0 | pass |  |
| suppliers: run 1 inserted all 9 rows into Postgres, run 2 changed none | pass | [(9, 0, 0), (0, 0, 9)] |
| suppliers: Postgres and SQLite hold the same 9 rows | pass | {'normalized JSONL': 9, 'SQLite': 9, 'Postgres': 9} |
| suppliers: every owned field agrees across the normalized JSONL, SQLite and Postgres | pass | 9 records, 216 field values compared, 0 mismatch(es) |
| suppliers: a fixed sample of 4 ids matches the oracle in Postgres | pass | 0 mismatch(es) |
| suppliers: the sensitive fields ['contact_email'] have no column in Postgres (the drop policy) | pass |  |

- materials: Postgres load (inserted, updated, unchanged), run 1 and 2: `[[24, 0, 0], [0, 0, 24]]`
- materials: rows per store: `{"Postgres": 24, "SQLite": 24, "normalized JSONL": 24}`
- materials: field comparison (Postgres): `{"field_values_compared": 768, "mismatch_count": 0, "mismatches_shown": [], "owned": ["code", "composition", "composition_raw", "extra", "fetched_at", "folder_paths", "images", "issues", "name", "price", "raw_ref", "schema_version", "source_version", "supplier", "weight_gsm", "width_cm"], "records": 24, "stores": ["normalized JSONL", "SQLite", "Postgres"]}`
- suppliers: Postgres load (inserted, updated, unchanged), run 1 and 2: `[[9, 0, 0], [0, 0, 9]]`
- suppliers: rows per store: `{"Postgres": 9, "SQLite": 9, "normalized JSONL": 9}`
- suppliers: field comparison (Postgres): `{"field_values_compared": 216, "mismatch_count": 0, "mismatches_shown": [], "owned": ["address", "certifications", "country", "fetched_at", "issues", "lead_time_days", "listed_at", "name", "rating", "raw_ref", "schema_version", "source_version"], "records": 9, "stores": ["normalized JSONL", "SQLite", "Postgres"]}`

##### S9: The real default workspace is untouched: PASS

| Check | Result | Observed |
|---|---|---|
| ~/.local/share/catalog-sync is unchanged | pass | did not exist before, still does not |
| no command fell back to the default workspace (HOME was a throw-away directory) | pass |  |
| the commands wrote to their own CATALOG_SYNC_HOME | pass | 14 of 14 workspace(s) created |

- what was fingerprinted (stat only; no file was opened): `["<home>/.local/share/catalog-sync"]`

##### dist-check: The built wheel and sdist hold nothing that must not ship: PASS

| Check | Result | Observed |
|---|---|---|
| forbidden-term: no forbidden term in a name or in content | pass |  |
| host-path: no home-directory path of a macOS or Linux user | pass |  |
| entry-points: only what the root pyproject registers, and no other package | pass |  |
| never-ship: no file that must never ship | pass |  |
| required-file: LICENSE, README.md and the licence metadata are there | pass |  |
| both archives were really read (a rule that looked at nothing proves nothing) | pass | {'wheel': 146, 'sdist': 394} |

- entries scanned: `{"sdist": 394, "wheel": 146}`
- private terms applied: `6`

##### licenses: The licence of every installed distribution is known and not copyleft: PASS

| Check | Result | Observed |
|---|---|---|
| the listing holds catalog-sync itself and at least one dependency (it looked) | pass | 10 distribution(s) |
| every installed distribution states a licence | pass |  |
| no copyleft licence (GPL, AGPL, LGPL, SSPL, EUPL, MPL) outside the reviewed exceptions | pass |  |

- distributions: `10`
- reviewed copyleft exceptions in use: `{"certifi": "file-level copyleft on a bundle of CA certificates, imported unmodified; requests needs it", "psycopg": "LGPL, imported unmodified and linked dynamically; the optional [postgres] extra only", "psycopg-binary": "LGPL, imported unmodified and linked dynamically; the optional [postgres] extra only"}`
- environment: `"the clean wheel environment, with the postgres extra"`

**Licences of the distributions installed in the clean wheel environment**

| Distribution | Version | Licence | Verdict |
| --- | --- | --- | --- |
| catalog-sync | 0.1.0rc1 | MIT | ok |
| certifi | 2026.10.23 | MPL-2.0 | reviewed exception |
| charset-normalizer | 3.5.2 | MIT | ok |
| idna | 3.20 | BSD-3-Clause | ok |
| pillow | 13.0.0 | MIT-CMU | ok |
| psycopg | 3.3.6 | LGPL-3.0-only | reviewed exception |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | reviewed exception |
| PyYAML | 6.0.3 | MIT | ok |
| requests | 2.34.2 | Apache-2.0 | ok |
| urllib3 | 2.8.0 | MIT | ok |

##### image: The container image holds the core package only and runs as non-root: PASS

| Check | Result | Observed |
|---|---|---|
| docker build of the HEAD export exits 0 | pass |  |
| no connector distribution: catalog-sync is the only distribution of that name | pass | catalog-sync |
| only the root pyproject's entry points are registered | pass |  |
| the image runs as a non-root user | pass | uid 10001, configured user '10001:10001' |
| no file under /app (the virtual environment included) is on the never-ship list | pass |  |
| its program reports the version of the source | pass | catalog-sync 0.1.0rc1 |
| the listing of the image's files was not empty | pass | 1094 entries |
| the image tag this run built is removed afterwards | pass | catalog-sync-accept-6eb6929d:rc |

- image size (bytes): `61295201`
- user: `{"configured": "10001:10001", "uid": 10001}`
- distributions: `["PyYAML 6.0.3", "catalog-sync 0.1.0rc1", "certifi 2026.10.23", "charset-normalizer 3.5.2", "idna 3.20", "pillow 13.0.0", "psycopg 3.3.6", "psycopg-binary 3.3.6", "requests 2.34.2", "typing_extensions 4.16.0", "urllib3 2.8.0"]`
- entries under /app: `1094`
- exported commit: `"500e66ecfe110bb841062723736d8dc67d3ab574"`
