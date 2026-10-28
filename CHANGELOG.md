# Changelog

Versions follow [PEP 440](https://peps.python.org/pep-0440/). Until 1.0 the API and the output
contract may change between releases.

## 0.1.0rc1 - 2026-10-27

The first release candidate.

### What v0 is

A framework and one fake service to run it against. The framework collects data from a service you
have an account on, keeps every raw response, normalizes it offline with a mapping you can read, and
loads it into your own platform as a partial upsert with a backup. Each pull is resumable, and every
run is written to an audit log. A source cannot be contacted until its config states who owns the
data, on what authority it is read and how fast the service may be called.

What is in the package:

- the pipeline and its commands (`pull`, `normalize`, `load`, `run`, `schedule`, `runs`, `login`,
  `init`, `new-connector` and the rest of `catalog-sync --help`);
- the SDK a connector is written against, and a scaffold that generates a connector with its own tests;
- five sinks: `jsonl`, `csv`, `sqlite`, `postgres` (the `postgres` extra) and `http`;
- the Mock SaaS and the reference platform, both of which run from the installed package on
  loopback and need no account;
- `replay`, a source that reads a stored snapshot and makes no request.

### Supported scope

What was run, on which Python and operating system, and what is not supported, is in
[docs/acceptance.md](docs/acceptance.md) ("Supported scope"), next to the record of the last
clean-install run.

### Output contract

The shape of a normalized record, how a missing or unparseable value is written, which fields a
load owns and which it keeps, and what happens on a re-import are in
[docs/data-contract.md](docs/data-contract.md). The platform side of the `http` sink is in
[docs/platform-contract.md](docs/platform-contract.md).

### Known limitations

From [docs/data-contract.md](docs/data-contract.md), section 14, as they stand in this release:

- `source` is the plugin name, so two entries of one plugin collide in one table.
- A schema has no required, non-null or enum concept; the only constraint is the key.
- Sensitive values are plain in the raw cache and in the normalized JSONL.
- A failed picture or detail request isolates the whole item, not only that field. After a detail
  failure the item's pictures are not fetched either, and `normalize` reports it as not fully pulled
  (assets, detail).
- `schema_version` is not enforced beyond rejecting a file whose version does not match.
- The pictures the Mock SaaS draws are encoded by Pillow, so the digests and sizes in the example
  follow the pinned Pillow version.

Outside the data contract:

- The clean-install acceptance harness has passed on macOS (Apple silicon, Python 3.13)
  and in GitHub CI on Linux (Python 3.12). The test matrix targets Python 3.11 to 3.13;
  consult the CI result for the exact commit being used.
- A request signed by JavaScript on the page, a site with no JSON behind it, a CAPTCHA and a second
  factor are not supported. A person does the last two.

### Connectors for real platforms

The public repository and release package contain the Mock SaaS and `replay`, and no connector
for a real service. Real-service connectors are maintained as separate packages. They are not
part of this repository, its wheel, sdist or image; the connector SDK and scaffold remain available
for users to build and test their own adapters.
Real-service connectors are not released here; this checkout has no `connectors/` directory.
