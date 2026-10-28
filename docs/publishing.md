# Before this repository is made public

This is the checklist for the maintainer who flips the repository from private to public. It exists
because the history of a repository is published with it: a value deleted in a later commit is still
there for anyone who clones. Do these in order; the first four are scans that must come back with
nothing.

Nothing here lists what is confidential. That list lives in a file **outside** the repository
(below); publishing it, even as a test fixture, would leak the thing it protects.

## 1. The private terms file

Make a plain text file somewhere that is not inside this repository (the scanner refuses a path
inside it), one confidential term per line: customer and company names, supplier names, real
catalogue codes and barcodes, folder or product names from the real data, personal names and
accounts. Blank lines and lines that start with `#` are ignored. A term needs at least two characters.

```console
$ chmod 600 ~/private/catalog-sync-terms.txt
```

Matching is literal, case-insensitive and Unicode-normalised, and a space inside a term matches any
run of spaces, underscores, hyphens and dots (or nothing), so `Acme Textile` also finds
`acme_textile`. Terms are not patterns. List variants that differ in other ways (an abbreviation, a
translation) as separate lines.

## 2. Scan everything with `scrub_scan`

```console
$ uv run python tools/scrub_scan.py --terms ~/private/catalog-sync-terms.txt
```

It looks in:

- the working tree: tracked files and untracked files that are not ignored, contents and file names;
- **every version of every file on every ref**, including files deleted long ago and lines edited
  out (what `git log -p --all` would show, checked once per distinct file version);
- commit messages, author and committer names and emails;
- ref names (branches, tags) and the messages and taggers of annotated tags.

Exit code `0` means nothing was found. `1` means findings: the output names each by the **line number
of the term in your terms file** and by where it was found (file and line, blob and the commits that
touched it, or commit and field). It never prints a term or the text around it, and masks any part of
a path that matches. `2` means the scan could not be done (unreadable terms file, not a git
repository, git failed), which is never to be read as a pass.

Use `--no-history` to scan only the working tree of a directory that is not a repository.

CI runs the same scan on every push when the repository secret `CATALOG_SYNC_PRIVATE_TERMS` holds the
terms (one per line); a fork's pull request cannot see the secret and skips it with a notice.

If something is found in history, deleting it in a new commit does **not** fix it. Rewrite the
history (step 7) before publishing.

## 3. Scan with gitleaks, including your private rules

The public `.gitleaks.toml` holds only generic rules (tokens, local paths, personal emails, stray
CJK text). Run it over the whole history:

```console
$ gitleaks detect --source . --config .gitleaks.toml --log-opts=--all --redact
```

To add your own rules on top without committing them, write a wrapper config outside the repository:

```toml
[extend]
path = "<absolute path to this repository's .gitleaks.toml>"

[[rules]]
id = "private-term"
regex = '''(?i)first-term|second-term'''
```

and pass it as `--config`. The CI workflow `scrub.yml` does the same with a repository secret, so a
pull request from a fork (which cannot see the secret) still runs the public rules.

## 4. The fixed greps

These should print nothing. They catch the classes of mistake that are cheap to make:

```console
$ git grep -nIE '/(Users|home)/[A-Za-z0-9._-]+/' $(git rev-list --all) -- . | head
$ git log --all --format='%ae%n%ce' | sort -u
```

The second command should list only the noreply address you intend to publish under.

## 5. Read, do not only scan

A scanner finds what it was told about. A person finds what nobody thought to list.

- Open every fixture under `tests/fixtures/` and `examples/`: all values must be obviously made up
  (`tools/make_synthetic.py` generates the example snapshot; regenerate it and check there is no diff).
- Read the README, `docs/`, `CONTRIBUTING.md`, the example config and the mapping examples for names,
  figures, URLs and wording that came from a real engagement.
- Read the commit messages (`git log --all`) once, top to bottom.
- Check the issue and pull-request templates, if any, and the CI workflows for secrets or internal
  hostnames.

## 6. The terms of service and the data owners

- Read the terms of service of **every service a connector talks to**. Write down your conclusion
  somewhere that is not in this repository. If the terms do not allow what the connector does, do not
  publish the connector: publish the framework only, and leave the connector's directory out.
  A connector is a self-contained package under `connectors/<name>/`, with its own `pyproject.toml`,
  entry points, tests and docs. It is not part of the main wheel, sdist or image (the wheel packages
  `src/catalog_sync` only, the sdist excludes `connectors/`, the `Dockerfile` copies nothing from it,
  and `tests/test_connector_boundary.py` checks all three), and nothing outside its directory names it.
  So it can be split out, to a private repository or left unreleased, by moving that one directory.
- **Release rule.** A third-party platform connector that has not passed a terms/authorization review
  (the conclusion you wrote down above: the terms allow this use, or the platform's owner authorized it)
  is excluded from the release package, from the entry points and from the demos. It is not in the wheel,
  the sdist or the image, the root `pyproject.toml` registers nothing for it, and no demo, example
  configuration or README code block uses it.
- Where a connector was built for a particular customer's data, tell that customer before the
  repository goes public, and keep their confirmation with the same record.
- Re-read the README's *Responsible use* section and `docs/compliance.md` as if you were a stranger.

## 7. If there has ever been a leak: new history

If any scan found a real value, or if you are not sure the history is clean, do not try to patch it.
Create a fresh repository from the current tree (a single new commit with the noreply identity),
run steps 2 to 4 on **that**, and publish it. Keep the old repository private or delete it. Anyone who
had access to the old history must be told it contained the value, and the value must be rotated if it
was a credential.

## 8. When flipping the switch

- Turn on GitHub's secret scanning and **push protection** for the repository before the first push
  to the public repository.
- Confirm the `scrub` workflow passes on the default branch and that the `CATALOG_SYNC_PRIVATE_RULES` and
  `CATALOG_SYNC_PRIVATE_TERMS` secrets are set, so that every later push is scanned for your private terms too.
- Confirm private vulnerability reporting is enabled (see `SECURITY.md`).
- Only then change the visibility.

After publication, run steps 2 and 3 again from time to time and whenever the terms list grows: a new
customer is a new term, and the history you published earlier is exactly what it needs to be checked
against.

## 9. Before each release: check what is inside it

A release is the main wheel, the sdist and the image. A rule about what they must not hold is
checked by looking inside them, not by reading the build configuration. Do this for every release
candidate and every release, from a clean checkout at the commit that will be tagged (the image is
built from `git archive HEAD`, so uncommitted files never reach it):

```console
$ uv run python tools/acceptance.py --out <new directory outside the repo> \
    --private-terms ~/private/catalog-sync-terms.txt [--postgres-dsn <throw-away Postgres>]
```

Besides the clean-install scenarios, the run records three release checks. Each one is a row of the
report and none may be `FAIL`; a `NOT_RUN` (for example `image` on a machine without Docker) must be
explained before the tag is made.

| Row | What it looks at | Fails when |
|---|---|---|
| `dist-check` | The wheel and the sdist the run built, with `tools/check_dist.py`. | A private term or a public pattern (a personal address, CJK text outside `README.zh-CN.md`, a token, a private key) is in a name or a file; a `/Users/<name>` or `/home/<name>` path is anywhere; an entry point or a package is not registered by the root `pyproject.toml`; a file from the list below is present; `LICENSE`, `README.md` (sdist) or the licence metadata (wheel) is missing. |
| `licenses` | Every distribution installed in the clean wheel environment (with the `postgres` extra when a DSN was given), as a table in the report. | A licence is copyleft (GPL, AGPL, LGPL, SSPL, EUPL, MPL) and not in the reviewed list, or a distribution states none. |
| `image` | The `Dockerfile` built from an export of HEAD, then run with no network. | A distribution named `catalog-sync*` other than the core is installed; an entry point is registered that the root `pyproject.toml` does not register; the user is root; a file under `/app` is on the list below; `--version` is not the source's. The tag it built is removed afterwards. |

`--private-terms` is the file of step 1 (or `$CATALOG_SYNC_PRIVATE_TERMS_FILE`). Without it
`dist-check` applies the public rules only, and the report says so. The file is never recorded and
no term is printed: a finding names a term by its line number, as `scrub_scan` does.

To look at a build without the whole run:

```console
$ uv build --out-dir <build dir outside the repo>
$ uv run python tools/check_dist.py --wheel <build dir>/catalog_sync-*.whl \
    --sdist <build dir>/catalog_sync-*.tar.gz --private-terms ~/private/catalog-sync-terms.txt
```

Exit code `0` is clean, `1` is findings, `2` is "could not look" (unreadable archive, a terms file
inside the repository), which is never a pass.

**Files that must never ship**, in the wheel, the sdist and the image: a `connectors/` directory,
the local agent-state directory (the one `.gitignore` lists under "Local agent worktrees and settings"), `.git/`, `runs/`, `out/`, browser profiles (`browser-profile/`, `.browser_profile/`),
`.env` and `.env.*`, `*curl*.txt`, `*.cred`, `*.token`, `*.db`, `*.db-journal`, `*.sqlite*`,
`.pre-commit-config.yaml`, and at the top level `cache/`, `secrets/`, `credentials/`, `creds/` and
`browser/`. The one exception is a file named `.env.example` that gives no variable a value
(`deploy/.env.example`, which `tests/test_deploy.py` reads): one with a value fails.

**The sdist.** It holds the tracked files of the repository minus three exclusions in
`pyproject.toml`: `/connectors` (packages released on their own, or not at all), the local
agent-state directory and `/.pre-commit-config.yaml` (the maintainer's hooks). hatchling also leaves out what
`.gitignore` ignores, so a `.env` or a pasted request capture in your working directory does not get
in; `check_dist` checks the result regardless. Everything else stays because a test or the image
build reads it: `tests/`, `tools/`, `examples/`, `deploy/`, `docs/`, `uv.lock` and the `Dockerfile`
(the image build), `.github/` (`tests/test_connector_boundary.py` and `tests/test_docs_policies.py`
read the CI workflow), `.gitleaks.toml` (`tests/test_gates.py`) and `.gitignore` and
`.dockerignore`. Exclude `.github/` only when no shipped test reads it any more.

**Licences.** The `licenses` row fails on copyleft except for the distributions in
`REVIEWED_COPYLEFT` (`tools/acceptance_release.py`): `certifi` (MPL-2.0, a bundle of CA certificates
that `requests` needs) and `psycopg` / `psycopg-binary` (LGPL-3.0, the optional `postgres` extra).
They show in the table as `reviewed exception`. Each entry names the one licence family it covers,
so a change to another family fails again. Add to the list only after reading the licence and
writing the reason down; the commit that adds an entry is the review.

By hand, the lists the checks are built on:

```console
$ unzip -l <build dir>/catalog_sync-*.whl
$ unzip -p <build dir>/catalog_sync-*.whl '*/entry_points.txt'
$ tar -tzf <build dir>/catalog_sync-*.tar.gz
```

- The wheel lists `catalog_sync/` and its `.dist-info` and nothing else, and every entry point in
  `entry_points.txt` points into `catalog_sync.`.
- The sdist has no `connectors/` and no local tool state; read the rest of the list for anything only
  your own machine has.
- The image's installed distributions are `catalog-sync` and its dependencies. A connector is added
  to an image by whoever deploys it, never by this one.
- Every name in these lists is either the core or a connector that passed the review in step 6.
