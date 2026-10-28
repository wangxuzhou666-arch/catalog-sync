# Deploying catalog-sync

How to run catalog-sync as a service: a scheduler in a container, next to the database and the
platform it loads into. The repository ships a compose stack for it, [`deploy/compose.yaml`](../deploy/compose.yaml),
that you can run in two minutes against the built-in mocks and then point at your own systems.

| File | What it is |
|---|---|
| [`deploy/compose.yaml`](../deploy/compose.yaml) | The stack: scheduler, Mock SaaS, reference platform, Postgres, and a login one-shot. |
| [`deploy/catalog-sync.compose.toml`](../deploy/catalog-sync.compose.toml) | The config the scheduler reads (mounted read-only at `/config/catalog-sync.toml`). |
| [`deploy/.env.example`](../deploy/.env.example) | The secrets the stack needs, as empty variables. Copy it to `deploy/.env`. |
| [`deploy/postgres-init.sh`](../deploy/postgres-init.sh) | Creates the restricted database role catalog-sync loads as. |
| [`Dockerfile`](../Dockerfile) | The image: non-root, no browser, no secret, bases pinned by digest. |

## The local stack, with the mocks

You need Docker with Compose v2. Run everything from the root of the repository.

```console
$ cp deploy/.env.example deploy/.env
$ chmod 600 deploy/.env
```

Give every variable in `deploy/.env` a value. `openssl rand -hex 16` makes a good one (hex keeps the
database password safe inside a connection string); `MOCK_SAAS_USER` can be any name. Compose refuses to
start while one is empty, so the stack never comes up with a forgotten secret. The file is git-ignored,
and it is the only place the secrets live.

Start the three services the scheduler depends on, and log in while they run:

```console
$ docker compose -f deploy/compose.yaml up -d mock-saas mock-platform postgres
$ docker compose -f deploy/compose.yaml run --rm login
```

`login` is the one-shot of the profile `tools`: it runs `catalog-sync login mock --form` in a container that
shares the scheduler's `/data`, stores the credential there, and is removed again. Then start the
scheduler:

```console
$ docker compose -f deploy/compose.yaml up -d
```

Log in **before** the scheduler starts, as above. Its first cycle runs at once; with no credential that
cycle ends with "expired or missing" and the next one is a whole `--every` away (one hour here).

The first cycle takes a few seconds. Check on it:

```console
$ docker compose -f deploy/compose.yaml exec catalog-sync catalog-sync health
healthy: the last successful scheduled cycle of 'mock-materials' finished 41s ago (limit 2h)
$ docker compose -f deploy/compose.yaml ps
$ docker compose -f deploy/compose.yaml logs catalog-sync
```

`health` is what Docker runs every minute to mark the container `healthy` or `unhealthy`; it reads the
audit log only. After a cycle in which some items failed, within the allowed share, it prints
`healthy, degraded: ...` instead and still exits 0. Both sinks received the 24 materials of the Mock SaaS;
look at them where they landed:

```console
$ docker compose -f deploy/compose.yaml exec postgres psql -U postgres -d catalog -c "select source_id, code, name, weight_gsm from catalog.materials order by source_id limit 3"
$ docker compose -f deploy/compose.yaml exec mock-platform python -c "import sqlite3; print(sqlite3.connect('/data/platform.db').execute('select source_id, code, name from material order by source_id limit 3').fetchall())"
$ docker compose -f deploy/compose.yaml exec catalog-sync catalog-sync audit show
```

The platform's table is named after the entity (`material`); the Postgres sink's after its `table` option.
The next cycle in an hour finds nothing changed and reports `unchanged 24`: a load is idempotent.

Stop it, and delete everything it stored:

```console
$ docker compose -f deploy/compose.yaml --profile tools down -v
```

Without `-v` the volumes stay, and so do the credential and the state: `up -d` continues where it stopped.

## How the pieces fit

```
  login (one-shot) ──┐
                     ├─ /data ─ state.sqlite, cache/, audit.jsonl, credentials/   (volume catalog-data)
  catalog-sync ──────┘   │
   schedule, every 1h    ├── reads  ──▶ mock-saas      (the service the data comes from)
                         ├── loads  ──▶ postgres       (role catalog_sync, schema catalog)
                         └── loads  ──▶ mock-platform  (the HTTP sink; its own volume)
```

- **One image, four containers.** The scheduler, the login one-shot and the two mocks all run the
  same image (`CATALOG_SYNC_IMAGE`, default `catalog-sync:dev`, built from the `Dockerfile` on the first
  `up`). Postgres is the official image, pinned by digest.
- **`/data` is the workspace** (`CATALOG_SYNC_HOME`). It holds everything catalog-sync owns: the state
  database, the raw cache, the audit log and the credentials (`credentials/`, readable by the container's
  user only). It is a named volume; the login one-shot mounts the same one, which is how the scheduler
  finds the credential it stored.
- **`/config/catalog-sync.toml` is the config**, mounted read-only. The image's working directory has a
  link to it, so no command needs `--config`. Relative paths in the file resolve against `/config`,
  which is why every other path is absolute. Nothing else is mounted next to it: a connector's default
  mapping is packaged with the connector, and the stack uses that one.
- **Secrets reach the containers as environment variables, one service at a time**: the scheduler gets the
  platform token and the database connection string, the login one-shot gets the account, Postgres gets its
  two passwords. The config names the variables (`dsn_env`, `auth_header_env`) and never holds a value.
- **No port is published.** The services reach each other by name on the stack's private networks: `sources`
  (the two mocks, the scheduler and the login one-shot) and `warehouse` (Postgres and the scheduler only; it has
  no way out). The mocks cannot reach the database. To look at the mock in a browser, add
  `ports: ["127.0.0.1:8765:8765"]` to `mock-saas`.

## The scheduler under Docker

The `catalog-sync` service runs `schedule mock-materials --every 1h` in the foreground. Four settings make
it behave under a supervisor:

| Setting | Why |
|---|---|
| `stop_grace_period: 15m` | A stop lets the cycle in progress finish before the process exits (the puller has no cancellation point). Docker kills it when the period is over; a killed run can be resumed with `pull --resume`, but give a real catalog the time. |
| `healthcheck` (`start_period: 5m`) | Healthy means a scheduled cycle finished successfully within twice its interval, plus the longest jitter the scheduler ran with. A cycle that finished with some failed items, within `--max-failed-ratio`, counts as successful (it is *degraded*, see below). The start period covers the first cycle. **Only the scheduler has one**: a container that runs anything else (the login one-shot, the mocks) must not inherit the image's health check, which would fail it. |
| `restart: on-failure:3` | See below. |
| `depends_on` with `service_healthy` | The first cycle starts when the source, the platform and the database answer. |

What the process exits with, and what the restart policy makes of it:

| Exit | Meaning | Under `on-failure:3` |
|---|---|---|
| 0 | Stopped by a signal, after the cycle in progress. | Not restarted. |
| 1 | Five cycles in a row failed for a reason other than the credential (`--max-consecutive-failures`). | Restarted, up to three times. |
| 4 | The compliance declaration is missing or invalid. No retry can fix it. | Restarted three times in a few seconds, then it stays down (`docker compose ps` shows `Exited (4)`). |

Exit 5, "completed with item failures", is what `run` and `schedule --once` exit with when a run finished but
some items failed, within `--max-failed-ratio`. The long-running scheduler never exits with it: it treats
that cycle as a success and goes on. You meet exit 5 in a one-shot `run` (a systemd service, a job runner).

Docker cannot tell exit 1 from exit 4, so the cap bounds both: after the third restart the container stays
down instead of looping, and the count is for the life of the container (a run that lasted does not reset
it). `docker compose up -d` starts it again once the cause is fixed. If you would rather have exit 1 restart
for ever, use `unless-stopped`, and accept that a bad compliance block then restarts in a loop; under
systemd the two can be told apart (see [Without Docker](#without-docker)).

A cycle that finishes with some items failed, no more than `--max-failed-ratio` of those it tried to fetch
(default 0.10; set it with `max_failed_ratio` in the profile or with `--max-failed-ratio` on `schedule`), is a
success marked **degraded**: no backoff, the count of failures in a row starts again, and `health` stays
healthy. The cycle's audit line has the outcome `ok_with_failures` and the log has a `warning:` with the
counts, so a person who wants to hear about failed items alerts on those, not on `health`. More failed items
than that make the cycle a failure like any other.

A cycle that fails for any other reason waits 1 minute, then 2, 4, ... never more than `--every`, and counts toward
the five. A cycle that finds the credential expired or missing does **not** count: it says so in the log, waits the
whole interval and tries again, which is the situation the next section is about.

## Pointing it at a real service and your own platform

The mocks are three services and one table in the config. To replace them:

1. **Copy `deploy/` to a directory of your own** (or keep editing a copy of the stack in a private
   repository), and delete the services `mock-saas` and `mock-platform`, the `depends_on` entries that name
   them, and the `mock-materials` source, credential and profile from the config.
2. **Write the config for your connector.** `catalog-sync init` on your workstation asks for every setting,
   checks each answer, logs in and pulls a small sample before it writes `catalog-sync.toml`; take that file
   and change what differs in the container: `[workspace] home = "/data"`, file sinks under `{workspace}/` (that
   is the volume) or at absolute paths, never at a path relative to the config, which is mounted read-only,
   and the mapping path (mount your mapping file next to the config, as the stack does). The compliance
   block is yours to fill in truthfully ([docs/compliance.md](compliance.md)); without a valid one the
   scheduler exits 4 before it makes a request.
3. **Point the sinks at your systems.** For your platform:

   ```toml
   [sinks.platform]
   kind = "http"
   url = "https://platform.example/api/ingest"
   auth_header_env = "PLATFORM_TOKEN"
   ```

   with `https://` and **without** `allow_plain_http`: the flag exists for the private network between
   containers, and a platform on a real network deserves TLS. What the platform has to implement is in
   [docs/platform-contract.md](platform-contract.md). For a database, a `postgres` sink with a `dsn_env` naming
   the variable that holds the connection string, as the stack does.
4. **Put the secrets where your platform keeps secrets.** The stack reads `deploy/.env`, which is fine for one
   machine. On an orchestrator, use its secret store and hand the values to the container as the environment
   variables the config names; nothing in the image or the config changes.
5. **Set the schedule and the profile** in the service's `command` (`schedule PROFILE --every 6h`, with
   `--jitter 10m` if many instances share a service) and in the health check (`--profile PROFILE`).
6. **Log in.** The scheduler cannot do it for you; see below.

### Credentials and their renewal

A connector's token lives in `/data/credentials` and expires; how often depends on the service, and
`auth status` says how many days are left. The cycle that finds it expired or missing exits 2 and says so in the
log; the scheduler does not stop, and does not count it as a failure. What you see from outside: no cycle succeeds,
so after twice the interval (plus the jitter) the container turns `unhealthy`. **Alert on that** (on the health
status of the container, or on `catalog-sync health` failing); it is the signal that a person has to log in.
Two warnings come earlier:
before every cycle the scheduler warns when a credential's `renew_before_days` is shorter than `--every`, and
`auth status` reports `renew soon`.

Renew it from outside the scheduler, in one of two ways. The scheduler's next cycle picks the new credential up;
no restart is needed:

```console
$ pbpaste | docker compose -f deploy/compose.yaml run --rm -T login <credential> --paste-curl -
```

- **`--paste-curl -`** reads a "Copy as cURL (bash)" from the browser's network tab on standard input
  (`<credential>` is the credential's name in the config)
  (`pbpaste` is the macOS clipboard; use `xclip -o` or a file elsewhere). A connector with an HTTP login
  takes `--form` instead (`run --rm login mock --form` is the stack's own), which reads the account from
  environment variables you add to the `login` service. A login that contacts the service (`--form`,
  `--browser`) is gated like a pull: the mounted config must hold a `[sources.*]` entry that uses this
  credential, with a valid compliance block, or it exits 4 before any request; its requests count against
  that source's `max_requests_per_minute`. `--paste-curl` makes no request and needs none.
- **`login --browser --headed`** needs a browser, which the image does not have. Run it on the host, against the
  same workspace. For that, give the service a directory of the host instead of the named volume
  (`- /srv/catalog-sync/data:/data`), create it for the container's user (`sudo install -d -o 10001 -g 10001
  -m 0700 /srv/catalog-sync/data`) and run the host's `catalog-sync --workspace /srv/catalog-sync/data login
  SERVICE --browser --headed`. Do that when a person has to type a code or solve a challenge: the tool never
  does either for you.

To not wait for the next cycle, restart the scheduler: it is asleep, so it stops at once, and its first cycle
runs at start-up.

```console
$ docker compose -f deploy/compose.yaml restart catalog-sync
```

### Where state and audit live, and backing up

Everything is in `/data` (the volume `catalog-sync_catalog-data` in the stack):

| Path | What it is |
|---|---|
| `state.sqlite` | Which runs exist, per-item state, what each pull has seen. |
| `cache/` | The raw responses of every run, other people's data, kept for `retention_days` and purged by `cache purge`. |
| `audit.jsonl` | What was done, one line per command and per scheduled cycle: counts and ids, never content or tokens. |
| `out/` | Where a file sink writes when its `path` starts with `{workspace}/` (the stack has none; a SQLite, JSONL or CSV sink you add would). Records, so other people's data. |
| `credentials/` | The stored logins. A secret. |
| `schedule-PROFILE.lock` | Held while a scheduler of that profile runs; a second one is refused. |

Back it up with the scheduler stopped (it finishes the cycle in progress, and the state database is then not
being written), as a plain archive, as root because the directory is the container user's alone:

```console
$ docker compose -f deploy/compose.yaml stop catalog-sync
$ docker run --rm --user 0 --entrypoint tar -v catalog-sync_catalog-data:/data:ro -v "$PWD":/backup catalog-sync:dev -czf /backup/catalog-data.tgz -C /data .
$ docker compose -f deploy/compose.yaml start catalog-sync
```

The archive contains the credentials and other people's data: keep it as private as the volume. To restore,
extract it into an empty volume the same way (`tar -xzf ... -C /data`, as root, which keeps the owners).
Two things are not in `/data` and are backed up separately: the warehouse (`pg_dump`; the `__backup_<timestamp>`
tables the Postgres sink makes before a load are a safety net for one load, not a backup) and the receiving
platform, whose own backups are the way back for the HTTP sink.

### Upgrading the image

The state stays in the volume; only the image changes:

```console
$ git pull
$ docker compose -f deploy/compose.yaml build
$ docker compose -f deploy/compose.yaml up -d
```

`up -d` recreates the containers whose image changed. The scheduler gets a stop signal, finishes the cycle in
progress (up to `stop_grace_period`) and starts again with the new image. Look at `ps` and `health` once a
cycle has run. To be able to go back, tag each build and name it in `deploy/.env` or the shell:
`docker build -t catalog-sync:2026-10-26 .`, then `CATALOG_SYNC_IMAGE=catalog-sync:2026-10-26 docker compose ... up -d`.

## Without Docker

The scheduler is one process; a unit file is all the supervisor it needs. Install the package into its own
environment (`pip install 'catalog-sync[postgres]'` into `/opt/catalog-sync`), create a user for it, and put the
config in `/etc/catalog-sync/` and the secrets in a file only that user can read (`EnvironmentFile`).

A timer that runs one profile once per cycle is the shorter way, and needs no long-running process:

```ini
# /etc/systemd/system/catalog-sync.service
[Unit]
Description=catalog-sync: pull, normalize and load one profile
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=catalog-sync
Environment=CATALOG_SYNC_HOME=/var/lib/catalog-sync
EnvironmentFile=/etc/catalog-sync/secrets.env
ExecStart=/opt/catalog-sync/bin/catalog-sync --config /etc/catalog-sync/catalog-sync.toml run weekly
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/catalog-sync
```

```ini
# /etc/systemd/system/catalog-sync.timer
[Unit]
Description=Run catalog-sync every hour

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now catalog-sync.timer` starts it. A run that fails leaves the service in the `failed`
state, with the exit code: 2 for an expired credential, 4 for a refused compliance declaration. `OnFailure=` can
name a unit that tells you. Exit 5 (a run that finished, with some items failed, within `--max-failed-ratio`)
also leaves the unit `failed`, because systemd calls every non-zero status a failure. If you would rather
treat a degraded run as a success, add `SuccessExitStatus=5` to the service; the audit line still says
`ok_with_failures`. More failed items than the ratio is exit 1 and stays a failure. To keep the long-running
scheduler instead, make the service `Type=simple` with
`ExecStart=... schedule weekly --every 1h`, `Restart=on-failure` and `RestartPreventExitStatus=4`: systemd then
restarts exit 1 and never exit 4, which a Docker restart policy cannot express.

## Security notes

- **Non-root, read-only, no capabilities.** The image runs as the numeric user `10001:10001`, and the stack
  sets `read_only: true` (the only writable places are the `/data` volume and a `tmpfs` at `/tmp`),
  `cap_drop: [ALL]` and `no-new-privileges`. A bind-mounted `/data` has to be owned by that user.
- **No secret in the image or in a file of the repository.** `.dockerignore` cuts the build context down and
  the `Dockerfile` copies only the files it names, so `docker history` shows none. The compose file refers to
  secrets as `${NAME:?...}`; their values live in `deploy/.env` (git-ignored, mode 0600) or in your secret store.
  A secret in the environment of a container can be read with `docker inspect` by anyone who can talk to the
  Docker socket, which is the same as root on that host: protect the socket as you would root.
- **Two private networks, and the database is on the smaller one.** No port is published. `warehouse` holds
  Postgres and the scheduler and is `internal` (no route to the internet or the host); `sources` holds the
  mocks, the scheduler and the login one-shot, so a compromised mock cannot connect to the database. The mocks
  listen on all interfaces of their network and refuse to start with their public demo password, hence the
  variables. Plain `http://` is accepted between containers only because `allow_plain_http = true` says so; use
  `https://` for anything that leaves the host. When you point the scheduler at a real service, that service
  is reached over `sources` (which does have a way out).
- **Sinks stay out of cloud-synced folders.** Never point sinks at a cloud-synced folder
  (iCloud/Dropbox/OneDrive), and do not bind-mount one as `/data` either: the records are other people's data,
  a sync client copies them to servers you did not choose, and it can copy a SQLite file in the middle of a
  write. Keep the workspace and the sink files on a disk that only your own backup touches.
- **Bounded containers.** Every service has `no-new-privileges` (Postgres included), a memory limit (512 MiB; 1 GiB
  for the scheduler, which resizes pictures, and for Postgres) and a process limit (128; 256 for Postgres). They
  are ceilings against a runaway process, not a sizing guide: raise them for a large catalog.
- **A least-privilege database role.** catalog-sync loads as `catalog_sync`: it can log in, and owns one schema
  (`catalog`) in which it creates its table and the `__backup_<timestamp>` tables. It is not a superuser and
  cannot create roles or databases, and has no rights on other schemas' tables. Never give the connection string of the
  administrator to the scheduler. The backup tables hold pre-load copies of rows: give them the access
  controls of the table.
- **Keep the dependencies updated.** Python dependencies are locked (`uv.lock`) and the base images are pinned
  by digest, so a rebuild gets the same bytes; the price is that nothing updates itself. Put a rebuild on a
  calendar: `uv lock --upgrade`, new digests (`docker buildx imagetools inspect python:3.12-slim`, and the
  same for the `uv` and `postgres` images), the test suite, the compose smoke job of CI, and a scan of the
  image with the scanner you use. A vulnerable dependency in a service that reads other people's data is a
  security finding, not a chore.
- **The compliance gate is not a deployment concern, and it is not optional.** An exit 4 means the declaration
  needs a human: fix it, do not loop on it.
