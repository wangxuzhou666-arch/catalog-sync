#!/bin/sh
# Run only the synthetic demo. Requires Docker and a POSIX shell; no host Python.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v docker >/dev/null 2>&1 || { echo 'Install Docker, then run this command again.' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo 'Start Docker, then run this command again.' >&2; exit 1; }
if [ -n "${DEMO_OUTPUT_DIR:-}" ]; then
    mkdir -p "$DEMO_OUTPUT_DIR"
    output=$(CDPATH= cd -- "$DEMO_OUTPUT_DIR" && pwd)
    [ -z "$(ls -A "$output")" ] || { echo 'DEMO_OUTPUT_DIR must be empty; existing files are never overwritten.' >&2; exit 1; }
else
    output=$(mktemp -d "${TMPDIR:-/tmp}/catalog-sync-demo.XXXXXX")
fi
container=
cleanup() {
    if [ -n "$container" ]; then docker rm -f -v "$container" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf '\nBuilding the demo image (the first run downloads dependencies)...\n'
docker build --tag catalog-sync:demo "$repo"
# No host credentials, Docker socket or network access are passed into this container.
container=$(docker create --network none --read-only --tmpfs /tmp:rw,nosuid,size=128m \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 1g --pids-limit 128 \
    --no-healthcheck --workdir /data --entrypoint python \
    --mount "type=bind,source=$repo/tools/demo.py,target=/demo/demo.py,readonly" \
    --mount "type=bind,source=$repo/catalog-sync.example.toml,target=/demo/template.toml,readonly" \
    catalog-sync:demo /demo/demo.py)
printf '\nRunning collection -> normalization -> SQLite + HTTP receiver...\n'
if ! docker start --attach "$container"; then
    echo 'Demo failed. See the step above; no successful report was produced.' >&2
    exit 1
fi
status=$(docker inspect --format '{{.State.ExitCode}}' "$container")
if [ "$status" != 0 ]; then echo "Demo failed (exit $status)." >&2; exit "$status"; fi
docker cp "$container:/data/report/." "$output/"
cleanup
container=
printf '\nPASS: report saved to %s/index.html\n' "$output"
printf 'Temporary container and its data volume removed. Only the report and cached image remain.\n'
printf 'To remove the cached image: docker image rm catalog-sync:demo\n'
if [ "${DEMO_OPEN_REPORT:-1}" != 0 ]; then
    if command -v open >/dev/null 2>&1; then open "$output/index.html" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$output/index.html" >/dev/null 2>&1 || true
    fi
fi
