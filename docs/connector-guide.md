# Writing a connector

A connector teaches catalog-sync to read one service. Whoever *uses* a connector that already exists
writes no code: they give the tool the service's address, a login, and the compliance block. This
page is for the person who writes the connector, and it is meant to take an afternoon:

```console
$ uv run catalog-sync new-connector acme
$ cd catalog-sync-acme && uv sync && uv run pytest -q     # green already, against a made-up service
```

then change what is marked `TODO(connector)` until the connector reads the real one. The steps:

1. [Decide whether it fits](#1-decide-whether-it-fits)
2. [Find the JSON endpoints](#2-find-the-json-endpoints-in-the-browser)
3. [Choose how it authenticates](#3-choose-how-it-authenticates)
4. [Generate the skeleton](#4-generate-the-skeleton)
5. [Fill in the endpoints, settings and field paths](#5-fill-in-the-endpoints-settings-and-field-paths)
6. [Declare the record and map it](#6-declare-the-record-and-map-it)
7. [Write synthetic fixtures](#7-write-synthetic-fixtures)
8. [Run the tests](#8-run-the-tests)
9. [Fill in the compliance block](#9-fill-in-the-compliance-block)
10. [First contact with the real service](#10-first-contact-with-the-real-service)

Then the [rules for contributors](#rules-for-contributors), the [limits](#what-this-cannot-do) and the
[SDK reference](#the-sdk-in-one-table).

## 1. Decide whether it fits

A connector fits a service whose web app gets its data from **JSON endpoints that your own login can
call**. Many SaaS tools whose web app calls JSON APIs are like that, and plenty are not. Before you start,
check three things.

- **You have the right to read it.** Your own account, or a customer's written authorization, or a
  contract, or the data is public. Step 9 asks you to write down which. If you cannot answer, stop
  here.
- **The terms of service allow automated access, or you have asked.** Read them now, not after the
  connector works. Note the date; the compliance block records it.
- **The endpoints are plain.** If the browser's requests carry a signature that JavaScript computes
  for each call, or the data only exists as rendered HTML, the SDK cannot help (see
  [What this cannot do](#what-this-cannot-do)).

## 2. Find the JSON endpoints in the browser

Log in to the service's web app as you normally do, then open the browser's developer tools
(F12, or right-click, Inspect) and go to the **Network** tab. Tick *Preserve log* and filter by
**Fetch/XHR**: those are the calls the page makes for data, without the pictures and scripts.

Click through what you want to export: open the list, scroll, open one item. For each call that
returns data, click it and read the *Headers* and *Response* panes. You are filling in this
worksheet:

| What | Where to look | Example (invented) |
|---|---|---|
| **List request** | The call that fires when the list appears or you scroll. Method, path, query or body. | `GET /api/v1/items?page=2&per_page=50` |
| **Pagination** | Which parameter changes between the first and second page, and what the response says about "more". | page number + a `total`; or a `next_cursor`; or `offset` and `limit` |
| **Item id** | The field every row has that is stable and unique. | `"id": "item-017"` |
| **Detail request** | The call that fires when you open one item. | `GET /api/v1/items/item-017` |
| **Envelope** | How answers are wrapped: the keys around the data, and how the service says it failed. | `{"state": true, "data": {...}}`, the detail twice (`data.data`) |
| **Change marker** | A field that changes when the item does (an update time, a version). | `"updated_at": "2026-01-05T09:00:00+00:00"` |
| **Pictures** | Where an image URL sits in the list row or the detail, and on which host the image lives. | `"image_url": "https://cdn.example/p/17.png"` |
| **Authentication** | What the calls carry that a logged-out browser would not. | an `Authorization: Bearer ...` header, a `Cookie`, an `x-api-key` |

Things worth noticing now, because they decide the shape of the connector:

- **List and detail rarely contain each other.** The listing often has the picture and a placeholder
  price of 0; the detail has the real price and no picture. The tool keeps the two records separate
  and the mapping reads both (step 6); never plan on merging them.
- **Totals can lie.** A total that is too high (rows removed while you page) must not loop on empty
  pages, and one that is too low must not drop rows. The page-number and offset walkers end a listing
  on an empty page, or on a short page at or past the reported total. A full page that reaches the
  total is therefore followed by one more request (a listing that ends exactly on a full page costs
  one extra, empty request), and a warning is logged when the rows delivered differ from the total.
  One case stays open: a service that caps the page size below what you asked for, together with a
  total that is too low, can still end the listing early, and the warning is how you notice. Your
  fake service should include one of these.
- **A folder tree** (the list mixes folders and items) needs the SDK's `FolderWalker` rather than a
  flat list. See [Folders](#folders-and-other-shapes).

**Do not save what you capture into your repository.** Not the responses, not a trimmed or
"anonymised" copy, not in a comment. Write down the *shape* (the worksheet above) and invent values
in step 7. "Copy as cURL" is used exactly once, to hand the tool a credential (step 3), and that text
goes to the tool's credential store, never to a file in the repository.

## 3. Choose how it authenticates

Look at the request headers of a data call in the Network tab and pick the row that matches. The
choice becomes `--auth` in step 4.

| You see | `--auth` | How the credential is obtained |
|---|---|---|
| `Authorization: Bearer ...` that the web app attaches after you log in | `bearer` | `login NAME --paste-curl -` (copy any data call as cURL and paste it) or `login NAME --browser`. A JWT's expiry is read, so `auth status` can warn before it lapses. |
| Only a `Cookie` carries the session | `cookie` | `login NAME --form`: the tool posts the login and keeps the one named session cookie. A pasted cURL cannot supply a cookie: the parser drops cookies on purpose. |
| A fixed key header such as `x-api-key`, issued in a settings page | `api_key` | `login NAME --paste-curl -` with a call that carries it, or write `API_KEY=...` into the `0600` credential file. There is no expiry to read. |
| A login request that answers with a token (JSON or a form post) | `form` | `login NAME --form`: posts the account and stores the token at a JSON path. No browser, so CI can do it. |

`--form` and `--browser` send requests (a password, then a check of the new token), so the tool holds
them to the compliance block of a source that uses this credential (step 9) and to its request ceiling;
without one they stop with exit code 4 before anything is sent. `--paste-curl` only reads text and is
not gated.

The account for `--form` and `--browser` comes from two environment variables (`<NAME>_USER` and
`<NAME>_PASSWORD`); the tool never reads a `.env` file. If the service asks for a person (a CAPTCHA, a
one-time code), a person logs in: `login NAME --browser --headed` opens a window and the tool only takes
the token. The SDK does not solve or bypass any of it.

## 4. Generate the skeleton

```console
$ uv run catalog-sync new-connector acme --auth bearer --pagination page
```

`NAME` is a lowercase identifier (`acme`, `acme_crm`), at most 20 characters, and not the name of an
installed connector. `--pagination` is `page` (page number with a total), `token` (next-page token) or
`offset` (offset and limit); `--dir` says where to write it (default `./catalog-sync-NAME`). The command
makes no request. It writes a standalone Python package.

Where the package gets catalog-sync from is `--catalog-sync-source`. Run from a source checkout (or an
editable install of one) the default, `auto`, writes a relative `[tool.uv.sources]` entry into the
package's `pyproject.toml`, so `uv sync` works in the fresh directory even while catalog-sync is not on
a package index; from an installed wheel it depends on the index alone, and the command says so when it
prints the next steps. `pypi` and `path` force either:

```
catalog-sync-acme/
  pyproject.toml                    its own entry points: source, credential kind, schema
  catalog-sync.example.toml         source, credential, sink and profile; the compliance block is placeholders
  README.md                         the checklist of this page, specialised to your choices
  src/catalog_sync_acme/
    settings.py                     [sources.NAME] options, validated
    api.py                          the calls and what a correct answer looks like
    credentials.py                  the credential kind: layout, login, live check
    source.py                       list, detail, pictures, inspect
    schemas/acme_item.yaml          what a record holds
    mappings/acme_item.yaml         which raw field feeds which record field
  tests/
    fake_service.py                 invented items and a fake service; the oracle
    test_connector.py               options, listing, detail, pictures, pipeline, compliance, packaging
    test_credentials.py             the credential kind's own cases
```

Install it where `catalog-sync` runs (`uv pip install -e .`) and the tool finds it by name:
`catalog-sync inspect acme-items` once the config has a `[sources.acme-items]` table. The core never
imports a connector; discovery is entry points only.

## 5. Fill in the endpoints, settings and field paths

`grep -rn "TODO(connector)" .` lists what is left. In order of how often it matters:

- **`api.py`**: `LIST_PATH` and `DETAIL_PATH`, the query parameters, and `parse_list_page`, which says
  where the rows (and the total or the next token) sit in the answer. If the service does not wrap
  answers as `{"state": true, "data": ...}`, read the block directly and drop `require_state_ok` and
  `peel_data`. A response that says it failed with HTTP 200 must be checked; the helper
  `require_state_ok` exists because "200" is not "success" on many services.
- **`settings.py`**: `DEFAULT_BASE_URL` (the web app's or API's address) and the page size the service
  allows. Every option is declared once, as a `SettingSpec` (key, type, default, a line of help;
  `essential=True` for what a person nearly always sets, such as the address), and the settings class
  reads through the same declarations, so the list cannot drift from the code. `item_connector()` in
  `__init__.py` publishes the list with a one-line summary, the credential kind's name and the mapping
  file, registered under `catalog_sync.connectors`: that is what `catalog-sync init` shows and asks, so
  someone using your connector never reads your code. Change the summary line (marked
  `TODO(connector)`); the generated test checks the declarations against the settings class.
- **`source.py`**: `ID_FIELD`, `VERSION_FIELD`, and `_PICTURES`, where each picture comes from
  (`list.<path>` or `detail.<path>`, first one with a URL wins). `https_only()` lets pictures come
  from any https host such as a CDN, but refuses `localhost` and loopback, private and link-local
  addresses (the cloud metadata address among them) unless you pass `allow_private_hosts=True`, for
  a service that really lives on a private network; `same_host(base_url)` allows only the API's own
  host. A picture URL that fails the rule is a reason to stop, not to fetch.
- **Redirects**: the rule also governs them. The HTTP client never lets the library follow a redirect.
  A redirect on an API call, or on a form post, is an error you surface (a POST is never re-sent: a
  login form must not carry its password to wherever a response points). A picture download follows at
  most 5 redirects, by hand, and every hop must pass the picture rule, takes its place in the rate
  window like any request, is never downgraded from https to http, and loses `Authorization` and
  `Cookie` once it is on another origin. Do not add redirect handling of your own.
- **`credentials.py`**: the login address and field names (`form`, `cookie`), the page and form
  selectors (`bearer` with `--browser`), or the header name (`api_key`).
- **The user agent**: leave it. The skeleton sends the SDK's `USER_AGENT`,
  `catalog-sync/<version> (+project URL)`, on API calls and picture requests: the tool, its version
  and where to find it, never a person or an account. A browser string is allowed only as a
  documented exception: state it, with the reason, under a "User-Agent policy" heading in your
  connector's own README, and test that the text is there (see
  [compliance.md](compliance.md#user-agent-policy); `catalog_sync.testing.user_agent` has the scan for
  a connector package's tests). A service that refuses the default is a reason to ask its owner, not to disguise the client. The headless browser
  of `login --browser` is not touched either: it announces itself, `HeadlessChrome` included, and a
  service that refuses that is served by `--headed`.

The SDK does the rest. Everything below is already wired in the skeleton; you meet it when something
about your service is unusual.

### Folders and other shapes

A service whose list mixes folders and items needs a walk down a tree, not one paged list. Use
`catalog_sync.sdk.FolderWalker`: it goes breadth-first, reports every item with the folder paths it was
found in, keeps its place in the run (`resume="page"` saves after every page, `resume="folder"` only
after a folder), refuses a tree deeper than `max_depth`, or a folder for which this run has fetched more
than `max_pages` pages (`FuseTripped`, exit code 3, resumable; a resumed run counts again from 0),
instead of silently stopping, and never lists a folder twice even when it is reachable by two routes or
links back to an ancestor (a folder already visited or queued is skipped before `max_depth` is looked
at, so a link back up the tree is not "deeper"). You give it `fetch_page(folder_id, page_number)`
returning a `RowPage`, `is_folder(row)`, and `WalkSettings(root_folder_id, max_depth, max_pages,
page_size)`, where `page_size` is the rows per page the fetcher asks for: it is how the walk tells a
full page, after which there may be more, from a short one. The mock connector is a worked example:
`catalog_sync/plugins/mock/source.py` (folders by `type`). A real service may mark a folder differently,
for instance a row without an item id; `is_folder` is where that rule goes.

A POST-with-JSON-body list (GraphQL, search endpoints) works the same way: `HttpClient.post_json` is
there, and the paginator you pick only needs a function from a position to a page.

### Showing up properly in `catalog-sync init`

`init` sets a source up without knowing its code, from what the connector says about itself. A connector
that says nothing still runs, but `init` lists it as "declares no settings" and the `[sources.NAME]`
table has to be written by hand. The skeleton declares everything below already; this is what each part
means, for when you add an option or write a connector without the skeleton.

One option is one `SettingSpec(key, kind, help, ...)`, next to the code that reads it, and the settings
class reads through the same object (`OptionReader.read(spec)`), so what `init` asks and what the code
accepts cannot differ. The `kind` is one of:

| `SettingKind` | What it is |
|---|---|
| `TEXT`, `INTEGER`, `NUMBER`, `BOOLEAN` | A plain value. `INTEGER` takes `minimum` and `maximum`. |
| `URL` | `http(s)://host[:port]`, no path: the address of a web app or an API. |
| `ENDPOINT` | A full `http(s)` URL, path allowed, no user name or password in it. |
| `CHOICE` | One of `choices` (and only a choice kind has them). |
| `ENV_NAME` | The **name** of an environment variable that holds a secret. Never the secret: `init` accepts capital letters, digits and underscores only, at most 64 (like `MY_SERVICE_TOKEN`), refuses a value shaped like a token without repeating it, and writes only the name. |
| `TABLE` | Structured (a nested table). Edited in the config file, never asked for. |

Two flags decide whether `init` asks: `required=True` (there is no usable default, so it has to be
given; a required option has no `default`) and `essential=True` (there is a default, but nearly everybody
sets it, such as the address). **Every other option keeps its default**; `init` does not ask, and you
change it in the config file afterwards. An option with no default that is not required (a token variable
the service may not need) can be left empty, and is then not written.

The list is published as a `ConnectorInfo`, returned by a function registered under the
`catalog_sync.connectors` entry-point group, keyed by the source's plugin name (`acme.items`, the
same key the skeleton registers under `catalog_sync.sources`; the `[sources.acme-items]` table of a
config is a different thing, an instance, and says `plugin = "acme.items"`):

```toml
[project.entry-points."catalog_sync.connectors"]
"acme.items" = "catalog_sync_acme:item_connector"
```

```python
def item_connector() -> ConnectorInfo:
    return ConnectorInfo(
        summary="Acme items, read through your own login",  # the one line `init` lists
        credential="acme",  # the credential kind the source's `credential` points at
        settings=SETTINGS,  # the SettingSpec list
        mapping=_HERE / "mappings" / "acme_item.yaml",
        credential_settings=("base_url",),  # asked once, written to the source and the credential
        terms_url="{base_url}/terms",  # optional: shown as a hint, never written unconfirmed
    )
```

`terms_url` is only a hint while the compliance block is filled in; the person confirms or replaces it,
and the review date is never set for them. Declaring a key twice, or naming a `credential_settings` key
that is not declared, fails when the info is built. The `ConnectorInfo` the skeleton generates has a test
that checks the declarations against the settings class; keep it passing when you add an option.

## 6. Declare the record and map it

`schemas/acme_item.yaml` says what a record of your connector holds, field by field, with a `kind`
(`text`, `integer`, `real`, `timestamp`, `json`). `mappings/acme_item.yaml` gives one rule per field:

```yaml
version: 1
source: acme.items
schema: ../schemas/acme_item.yaml
fields:
  name:
    from: [detail.name, list.name]        # the first source with a value wins
  amount:
    from: [detail.amount]
  contact:
    from: [detail.contact_email]
    sensitive: true                       # sinks drop it unless told to hash or keep it
  updated_at:
    from: [list.updated_at]
```

`list.<path>` is the listing row exactly as the service returned it and `detail.<path>` the detail record.
They are **never merged into one object**, so an empty value in one cannot wipe a value in the other. A value
that cannot be understood becomes `null` plus a note in `issues` (`weight_gsm:source_zero`,
`composition:unknown_code:X`), never a guess. Mark a field `sensitive: true` and sinks drop it unless told
otherwise. The parsers (`text`, `list`, `split`, `gsm`, `width_cm`, `number`, `integer`, `timestamp`,
`money`, `composition`, `raw`; the numeric ones take `zero_is_missing`) and code tables are described in
the README.

Run `catalog-sync normalize` as often as you like while you work on the mapping: it reads the cache and
makes no request.

## 7. Write synthetic fixtures

`tests/fake_service.py` is the heart of the skeleton. It is two things: an **invented dataset** (a few
items with obviously fake names and values) and a **fake service** that answers the way your connector
expects, driven through the shared fake transport. No socket is opened.

When you change a path, a field name or the envelope in `api.py`, change the fake the same way, then keep
the tests green. Then make the fake as nasty as the real service turns out to be. The cases worth a test
because services really do them:

- the listing says a price of 0 and has the picture; the detail has the price and an empty `thumbs` list;
- a total that is too high or too low; a page that comes back empty early;
- a token that is handed out twice; an offset the service ignores;
- a picture on another host; a signed picture link that expires (403);
- a 429 with `Retry-After`; an occasional 503; an expired token (401, never retried);
- a detail that 404s for one item while the rest are fine.

Two helpers from `catalog_sync.testing` carry the pattern. `ServiceWorld` wires a service function and the
client to **one clock**: when the client waits (a backoff, a `Retry-After`, the gap a rate ceiling implies)
the service's clock moves the same amount, so token lifetimes and rate windows behave as they would in real
time, and the test takes none. `pull_and_normalize` runs a source through pull and normalize in a temporary
directory. The skeleton's pipeline test is the **dataset as oracle**: an `expected_record(item)` function
derives, from one invented item, what a correct run must have stored, and the test asserts that for every
item. The expectation is computed from the data, never typed out, so a wrong mapping or a wrong parse shows
up as a difference.

**Never put a captured response in a fixture.** Not whole, not trimmed, not with the values changed. The
rule is the project's, and review looks for it.

## 8. Run the tests

```console
$ uv run pytest -q
$ uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src
```

All of it is green when you generate the package, and `new-connector` is tested to keep it that way for
every combination of `--auth` and `--pagination`. The generated tests cover: the options and their errors,
the whole listing in pages, the change marker, the detail, a missing detail failing only its item, the
picture being fetched without the credential, `inspect`, an expired credential stopping the run after one
request, the full pull-and-normalize against the oracle, the credential's own cases, that every
compliance placeholder alone is enough for the gate to refuse, and that every entry point in `pyproject.toml`
resolves.

A rule you add to the connector needs a test that makes it fail, not only one that makes it pass (the
project's gate rule, in [CONTRIBUTING.md](../CONTRIBUTING.md)).

## 9. Fill in the compliance block

`catalog-sync.example.toml` ships a `[sources.NAME-items.compliance]` table whose five human facts are
placeholders. The compliance gate refuses them (exit code 4, before a credential is read or a request is
made) until each is real:

| Key | What to write |
|---|---|
| `data_owner` | The organisation whose data this is. |
| `authorization` | `own_account`, `customer_authorized`, `contract` or `public_data`. |
| `authorization_ref` | Where the grant is recorded: a ticket, a wiki page, a contract number. A pointer, not the content. |
| `tos_url`, `tos_reviewed_on` | The terms you read and the day you read them. |
| `max_requests_per_minute` | A ceiling the service can take; the skeleton starts at a conservative 60. It holds for every request, retries included. |
| `retention_days` | How long the raw cache may be kept. |

The same block gates `login --form` and `login --browser`: a source whose `credential` is the one being
logged into has to have a valid one, and the lowest `max_requests_per_minute` among those sources paces
the login. A credential no source uses is refused (exit code 4). `login --paste-curl` is not gated.

The tool checks that the block is there and consistent. It cannot check that it is true: what that leaves
to you is in [compliance.md](compliance.md).

## 10. First contact with the real service

Everything before this point used invented data. Now, with your own account.

`catalog-sync init` walks through this step interactively once your connector is installed and declares
itself (see "Showing up properly in `catalog-sync init`" in step 5): pick the connector, answer the
compliance questions, log in, look at a sample of normalised records, choose a sink, and get a config
file written. The sample is a real pull against the real service. By hand, the same is:

```console
$ uv run catalog-sync login acme --paste-curl -          # or --form / --browser, as step 3 chose
$ uv run catalog-sync auth status acme
$ uv run catalog-sync inspect acme-items                 # a couple of requests; nothing is stored
$ uv run catalog-sync pull acme-items --max-items 5
$ uv run catalog-sync runs show <RUN>
$ uv run catalog-sync normalize acme-items --mapping src/catalog_sync_acme/mappings/acme_item.yaml --out out/acme.jsonl
```

The normalized file holds real data. `out/` is git-ignored in the generated package, but a folder that a
cloud service syncs (iCloud, Dropbox, OneDrive) is still not a place for it: write it in the workspace,
or somewhere you control.

Compare against the web app: the total `inspect` reports should match what the app shows; the five items
should have the fields you expect; `runs show` lists any item that failed and why (a `pull` with failed
items exits 5, "completed with item failures", and a `run` does too while the failures stay within
`--max-failed-ratio`). Then `pull` everything,
kill it half way and `pull --resume <RUN>` to see that it continues; pull again and see that unchanged items
cost no detail request. Say in your connector's README what you verified against the real service and what
only ran against the fake. Keep that as a live checklist: a connector that talks to a real service keeps
its own live checklist in its package docs.

## Rules for contributors

These are the project's rules for every connector, public or private, enforced in code where code can and
in review where it cannot.

- **No anti-detection.** No flags or scripts that hide that a browser is automated.
- **No rotation or disguise.** No user-agent or IP rotation, no proxies, no pool of identities. The SDK's
  default user agent names the tool and its version (`catalog-sync/<version> (+project URL)`), never a
  person, an account or an email. A connector sends it unchanged unless it has a documented exception:
  a browser string needs a row with its reason in the User-Agent policy table of
  [compliance.md](compliance.md#user-agent-policy), and the repository's tests fail on one that is not
  listed.
- **Redirects are not chased.** Do not turn on the library's redirect following or write a loop of your
  own. An API call that is redirected is an error; a picture download follows a redirect only through
  the picture rule (at most 5 hops, each checked and paced, never to http, no credential to another
  origin).
- **No CAPTCHA solving.** If a login needs a person, a person logs in.
- **No reverse-engineering of request signatures.** If the service signs its requests with something the
  page's JavaScript computes, that is the service saying it does not want scripted access. It is not a
  puzzle for this project.
- **Back off, do not push.** The shared `HttpClient` retries 5xx and 429 with exponential backoff, honours
  `Retry-After`, never retries another 4xx, and holds the declared ceiling. Do not write your own request
  loop.
- **Stop on a refusal.** A 401 or 403 is `AuthExpired` (exit code 2), not a reason to try something else.
- **Only what your account can see.** No guessing identifiers, no probing for endpoints.
- **Invented fixtures only.** Never a captured response, a token, a cookie or an account.
- **Fail honestly.** `AuthExpired` for a rejected credential, `ItemFailed` for one bad item (the run
  goes on and ends with exit code 5, or 1 if more than `--max-failed-ratio` of the fetched items failed),
  `SourceShapeError` for an answer of the wrong shape; a value you cannot understand is `null` plus an
  issue, never invented.
- **Say what is verified.** Which parts only ran against fake data, and which against the real service.

The reasons and the other half (what remains the operator's responsibility) are in
[compliance.md](compliance.md), and the contribution checklist in [CONTRIBUTING.md](../CONTRIBUTING.md).

## What this cannot do

Be honest with yourself about these before you start; a connector for such a service is a different, hand-
written project, and for some of them the answer is "not with this tool".

- **Requests signed by JavaScript.** If every call carries a signature, nonce or token that the page's code
  computes, the SDK cannot make the call without re-implementing that code, which this project does not do.
- **HTML-only sites.** The SDK reads JSON. A page that is rendered on the server and has no JSON behind it is
  scraping, not integration, and is out of scope.
- **CAPTCHAs and second factors.** A person does them; the tool never bypasses them. `--headed` exists for
  exactly that.
- **Single sign-on without a token you can hold.** If login ends in a session the tool cannot capture
  (cookies set by a redirect chain, for instance), only `--paste-curl` of a bearer-carrying call or `--form`
  against a login endpoint will work, and some services offer neither.
- **Pictures are the only binary assets.** Other downloads (PDF, 3D files) need your own `fetch_asset`.
- **Real-time or push.** The tool pulls on a schedule you run; it does not subscribe.

## The SDK in one table

`catalog_sync.sdk` is what the shipped connectors and the scaffold share. A piece that only one
connector uses stays here when the scaffold uses it too: the scaffold is a tested second consumer.
Everything else a connector needs (`HttpClient`, the Source contract, the errors) stays in
`catalog_sync`.

| Module | What it gives you |
|---|---|
| `options` | `OptionReader`: typed `[sources.NAME]` options read against their `SettingSpec` declaration (`read`, `integer_of`, `url_of`, ...), `reject_unknown`, the request-ceiling and gap rule (`pacing`). `SettingSpec`, `SettingKind` and `ConnectorInfo` are the public declaration `init` asks from. |
| `connection` | `Connection`: the credentialed API client and the anonymous asset client behind one shared rate window; created on first use. `USER_AGENT` is the default user agent. |
| `paging` | `rows_page`/`token_page`/`offset_page` to check a response block, and `walk_number_pages(fetch, state, *, key, page_size, max_pages, what)`, `walk_token_pages(fetch, state, *, key, max_pages, what)` and `walk_offset_pages(fetch, state, *, key, limit, max_pages, what)` to follow it with a saved position. `PageCursor(page_size, next_page=1, rows_seen=0)` is the page-number stop rule alone: a listing ends on an empty page, or on a short page at or past the reported total, and a warning is logged when the rows delivered differ from the total. `max_pages` is a fuse on the pages this process fetches for one listing, in every walker: more than that raises `FuseTripped` (exit code 3, resumable) and a resumed run counts again from 0. In the page-number and offset walkers the extra request after a full last page counts too, so a listing of exactly `max_pages` full pages trips it. |
| `folders` | `FolderWalker(fetch_page, settings, *, frontier_key, is_folder, resume="page")`, `Sighting`, `WalkSettings(root_folder_id, max_depth, max_pages, page_size)`, `folder_label`. The same `max_pages` fuse applies to each folder. |
| `responses` | `shape_context` (say which call a wrong answer belonged to), `item_scoped` (a 4xx about one item fails that item), `parse_detail` (the record under `data.data`). |
| `assets` | `PictureAssets` with `https_only(*, allow_private_hosts=False)` / `same_host()`: URLs from a row and its detail, stored without the query string, fetched without the credential, fitted into a byte budget. The rule also decides which redirects a download may follow. |
| `credentials` | `header_layout`, `EnvAccount`, `FormLogin` (JSON or form post; token at a path, or a session cookie), `verify_with_probe`. |

Test helpers are in `catalog_sync.testing`: `ServiceWorld`, `FakeTransport` and `SimulatedCrash` (a kill
nothing catches, for `crash_on_request`), `picked`/`sorted_issues` (the oracle comparison),
`pull_and_normalize`, and `sink_contract` for sinks. Three more are for a connector shipped as a package of its
own, whose tests cannot import this repository's `tests/`:

- `workspace_guard`: `RealWorkspaceGuard` fails the pytest session if the real default workspace was
  touched. Register it in `pytest_configure` of your `conftest.py`, next to an autouse fixture that points
  `CATALOG_SYNC_HOME` at a fresh directory for each test.
- `boundaries`: `python_files`, `reaches` and `other_plugins_reached` read a package's imports, so a test
  can assert that the connector uses `catalog_sync.sdk` and reaches into no built-in connector.
- `user_agent`: `connectors_sending_a_browser_string` finds a package that sends a browser's `User-Agent`
  and `policy_section` reads the "User-Agent policy" part of a document, so a test can assert that the
  exception is documented (the policy is in `docs/compliance.md`).
