# Compliance

catalog-sync copies data out of services that you, or your customer, use. A tool cannot make that
lawful or permitted; only the people who run it can. What the tool can do is turn the checks that
are easy to forget into code that refuses to proceed, leave a record of what was done, and say
plainly what it does **not** do. This page is that statement.

The first table splits the work. The left column is behaviour you can rely on because the code
enforces it (and a test proves it); the right column is what stays with whoever operates the tool.
Nothing in the right column is checked by the tool, and the tool cannot tell whether you did it.

| Topic | What the tool enforces | What remains your responsibility |
|---|---|---|
| **Declaration** | Every source that contacts a service needs a complete `[sources.NAME.compliance]` table. Without a valid one, `pull`, `inspect` and `run` stop with exit code 4 *before* a credential is read or a request is made. So does `login --form` and `login --browser`: a login posts a password and makes a request of its own, so it is held to the declaration of the source(s) whose `credential` it renews (the lowest `max_requests_per_minute` among them), and a credential that no source uses is refused (exit 4). `login --paste-curl` only reads text and needs none. `catalog-sync init` asks for the fields one at a time with an explanation of each, checks every answer by the same rules, and never picks the review date for you: it is today only after you have said you read the terms. | That the declaration is true: that you named the right data owner, that the authorization exists, and that `authorization_ref` points at a real record someone can find. |
| **Authorization** | `authorization` must be one of `own_account`, `customer_authorized`, `contract`, `public_data`. `authorization_ref` is required and must be a real string (placeholders such as `TODO` or `<ref>` are refused). | Getting the authorization: the account holder's or customer's consent, the data-owner agreement, the contract terms. The tool stores a pointer, never the document. |
| **Terms of service** | `tos_url` and `tos_reviewed_on` are required; a review older than `tos_review_max_age_days` (default 180) prints a warning on every pull. | Actually reading the terms and deciding whether automated access is permitted. Re-reading them when they change. The date is your word. |
| **Request rate** | `max_requests_per_minute` is enforced as a sliding-window ceiling on every request of the source, retries and redirect hops included, and on the requests of a login (the form post and the connector's check of the new token). A `request_gap_s` shorter than 60 divided by the ceiling is refused. A 429 is backed off from (see below). | Choosing a ceiling the service actually allows. The tool will hold whatever number you declare, even one the vendor forbids. |
| **Retention** | `retention_days` is required. `run` warns when the raw cache is older. `cache purge` deletes runs past the declared period and refuses to delete a run that unchanged items still depend on (it lists them instead). | Picking a period that is lawful for the data (contract, privacy law). Running `cache purge` (nothing runs it for you). Deleting what the tool does not touch: loaded rows, sink backups, exports you made. |
| **Audit trail** | Every login, pull, normalize, load, run and purge appends one line to `<workspace>/audit.jsonl`: time, command, source, run id, counts, the sha256 of the declaration in force, a fingerprint of the credential, the sinks written. It never holds a token, a URL, a record or a personal value. | Keeping the file, reviewing it, and protecting it. It is append-only by convention, not tamper-proof: anyone with write access to the workspace can edit it. |
| **Sensitive fields** | A mapping can mark a field `sensitive: true`. Sinks **drop** such fields by default; `hash` writes a salted sha256 (stable within the workspace); `keep` must be chosen explicitly. | Deciding which fields are personal or sensitive, having a lawful basis to keep them, and answering requests from the people they describe. Hashing is pseudonymisation, not anonymisation. The workspace salt cannot be rotated (see *Known limits*). |
| **Credentials** | Read only from environment variables or `0600` files inside the workspace; never from the config file or a `.env` file; never written to logs, errors, snapshots or the audit log (a masking layer catches a secret that slips into a message). | Using your own account, not a shared or borrowed one. Rotating and revoking. Keeping the workspace out of repositories, backups you do not control, and shared drives. |
| **Writes** | A load only writes the fields the mapping owns, takes a backup first, never deletes a row, and `--dry-run` writes nothing. | Access control and encryption at the destination, deleting data when asked to, and the retention of the backups the tool leaves behind (it never removes them). |
| **Where the data goes** | The HTTP sink reads its token only from the environment variable `auth_header_env` names (a config or URL that carries one is refused, and the refusal does not repeat it), accepts plain `http://` for loopback only, never follows a redirect with the token, and applies the sensitive-field policy before a request is built. `catalog-sync init` writes no secret and records only the names of variables. | That the platform you send to is yours, or that you may send this data there. Its access control, encryption, retention and backups: the sink takes no backup because the rows are not on its side, and what the platform does with a request is the platform's. |
| **Third-party data** | `.gitignore` excludes the workspace artefacts, and the project's own history is scanned for confidential terms before any publication. | The licence of the data. The cache holds copies of someone else's content: do not commit, publish or share it without the right to. |

## What a declaration looks like

```toml
[sources.my-source.compliance]
data_owner = "Example Co"                    # whose data this is
authorization = "own_account"                # own_account | customer_authorized | contract | public_data
authorization_ref = "wiki/data-access/12"    # where the grant is recorded: a pointer, not its content
tos_url = "https://vendor.example/terms"     # the terms you reviewed
tos_reviewed_on = 2026-10-26                 # when you reviewed them
max_requests_per_minute = 120                # the ceiling the tool will hold
retention_days = 90                          # how long the raw cache may be kept
# tos_review_max_age_days = 180              # how old the review may get before a warning
```

A source that only reads local files and makes no request (the built-in `replay` source) is exempt;
it is still written to the audit log. A source is exempt only if its plugin registers itself under
the `catalog_sync.local_sources` entry-point group; everything else is treated as a service.

The built-in Mock SaaS sources (`mock.materials`, `mock.suppliers`) are *not* exempt, although the
service they read is a fake running on your own machine: they make real HTTP calls, so they are gated
like any service. That is deliberate. The example config fills in a declaration for them (its terms
are the page the mock serves at `/terms`), and removing that block is the quickest way to see the
gate refuse a pull with exit code 4 before a single request is made.

## What the tool does not do: non-evasion

A compliance layer is worth nothing if the client is built to dodge the service's own defences. These
are rules of the project, kept in code and in review:

- **No anti-detection.** No flags or scripts that hide that a browser is automated. The browser used
  for `login --browser` is started with its normal settings and announces itself with its own
  user-agent, `HeadlessChrome` included; the tool never overrides it. A service that refuses a
  headless login is served by `login --browser --headed`, a visible window in which a person logs in.
- **No rotation, honest identification.** The SDK's default `User-Agent` is
  `catalog-sync/<version> (+<project URL>)`: the tool, its version and where to find it, never a
  person, an email or an account id. It is fixed and never rotated. There is no proxy, no IP
  rotation, no pool of identities. Redirects are not followed blindly either (see *Redirects*
  below). A connector may send a browser user-agent instead **only** as a documented exception:
  see [*User-Agent policy*](#user-agent-policy).
- **Back off, do not push.** HTTP 429 and 5xx responses are retried with exponential backoff, and a
  `Retry-After` header is honoured (capped). Other 4xx responses are final and are not retried.
- **No CAPTCHA solving.** If a login needs a person (a CAPTCHA, a one-time code), the tool stops;
  `login --browser --headed` opens a visible window where the person logs in once and the tool only
  takes the resulting token.
- **Stop on a refusal.** A 401 or 403 ends the run with exit code 2 and an instruction to log in
  again. Nothing retries a rejected credential in a loop.
- **Only what your account can see.** The tool reads through your own login. It does not probe for
  endpoints, guess identifiers or widen what an account may read.

Connector authors are held to the same list; see [CONTRIBUTING.md](../CONTRIBUTING.md).

### User-Agent policy

One policy for every connector: send the SDK default (`catalog-sync/<version> (+<project URL>)`).
A connector may send a browser string only if it states the reason where an operator reading its
documentation can see it. The connectors shipped inside catalog-sync send the default and have no
exception to list; a connector shipped as a package of its own lists its exceptions in its own
README, under a "User-Agent policy" heading (what it sends, and why), and its own tests check that
the text is there. The test suite fails when a connector shipped here carries a browser string
that this page does not list, and `catalog_sync.testing.user_agent` has the same scan for a
connector package's tests.

### Redirects

The HTTP client never lets the library follow a redirect. A response that redirects an API call or
a form post is an error (a POST is never re-sent: a login form must not carry its password to
wherever a response points). A picture download follows at most 5 redirects, by hand, and only to
URLs the connector's picture rule accepts: every hop is checked against that rule, takes its place
in the rate window like any request, is never downgraded from https to http, and is sent without
`Authorization` or `Cookie` once it is on another origin. `https_only()`, the rule the shipped
connector uses, also refuses `localhost`, loopback, private and link-local addresses (the cloud
metadata address among them), in any numeric spelling.

One honest caveat: a connector may talk to the internal API that a vendor's web app uses, because
the service offers no public export. Whether the vendor's terms allow that use is exactly the
question `tos_reviewed_on` exists to record. Read them first. A connector for a service whose terms
nobody has reviewed ships as an example with the status "needs live verification", in its own README.

## Known limits

- **The hash salt cannot be rotated.** `sensitive_fields = "hash"` mixes a per-workspace salt
  (`<workspace>/secrets/hash.salt`, created once) into every hash. Changing it would change every
  stored hash, and there is no command to migrate them. Treat the salt like a credential: if it is
  exposed, drop and reload the hashed columns with a new workspace.
- **The audit log is not tamper-evident.** It is written with `O_APPEND` and `0600`, and nothing in
  the tool rewrites it, but a person with access to the workspace can. Ship it to a store that is
  append-only if you need that.
- **Purging covers the raw cache only.** Rows already loaded into a sink, and the backups a load
  leaves behind, are yours to manage.
- **The clock is the local clock.** Ages, review dates and retention are computed from it.
- **A browser login is paced only where the tool makes the request.** The page loads and the API
  calls a headless browser fires on its own are not routed through the rate window; only the
  connector's check of the captured token is.
- **A host name that resolves to a private address is not caught.** The picture rule looks at the
  URL's text. Run the tool where it cannot reach what a lookup could point at, if that matters.
- **The declaration is not verified.** The tool checks that it is present, well-formed and
  consistent with the pacing. It cannot check that it is honest.
