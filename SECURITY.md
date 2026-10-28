# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository (Security tab, "Report a vulnerability"). Do not open a public issue for anything that could expose credentials or third-party data. You should get an acknowledgement within a few days; this is a spare-time project, so fixes are best effort.

## What counts as a vulnerability here

- A code path that writes a token, password, cookie or other credential to a log, a snapshot, an error message, an audit line or an output file.
- A code path that sends credentials, or anything identifying the operator, to a host other than the one the plugin is documented to talk to.
- Path traversal or arbitrary file write or delete through an identifier or file name that comes from a remote service (the cache purge, the snapshot store and the sinks all take such names).
- A way to make `catalog-sync` delete or overwrite data in a sink that the sink contract promises it will not touch, or to purge a cached run that unchanged items still depend on.
- Spreadsheet formula injection through a CSV export, or SQL injection through a table, schema or field name.
- A way to get the compliance gate to let a request through that the declaration forbids (a missing declaration, or a request rate above the declared ceiling).

## Handling of secrets

- Credentials come from environment variables or from `0600` files inside the workspace, which lives outside the repository. The config file never holds one (a Postgres sink reads its connection string from an environment variable named in the config, and refuses an inline one).
- Every log record of the package, every `error:` line, every stored item failure and every line that reaches standard error is passed through a masking layer (`src/catalog_sync/redact.py`): JWTs, bearer credentials, `Authorization` / `Cookie` header values, URL query strings, `token=` / `password=` pairs and the password in a `scheme://user:password@host` string. It is a second line of defence behind code that is written not to log secrets, and a test pushes a canary token through every log path to keep both honest.
- The workspace cache holds raw responses from third-party services. Treat it as sensitive and never commit it; `.gitignore` excludes the usual locations, and CI scans the full git history with gitleaks. `catalog-sync cache purge` removes runs past the retention you declared.
- If you find a secret in this repository's history, report it privately so that it can be rotated before the history is rewritten.

## Known limitations

- The audit log is append-only by convention, not tamper-evident.
- The salt behind `sensitive_fields = "hash"` cannot be rotated.
- The masking layer recognises secrets by their shape; a secret with no recognisable shape in a free-text message is not found.

See [docs/compliance.md](docs/compliance.md) for the compliance model and [docs/publishing.md](docs/publishing.md) for the checks that run before this repository is made public.
