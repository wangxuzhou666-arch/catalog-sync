#!/bin/sh
# Runs once, on the first start of an empty Postgres volume (docker-entrypoint-initdb.d).
# Creates the role catalog-sync loads as: it can log in and own one schema, and nothing else. It is
# not a superuser, cannot create roles or databases, and cannot touch other schemas' tables.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$CATALOG_SYNC_DB_PASSWORD" <<'SQL'
CREATE ROLE catalog_sync LOGIN PASSWORD :'app_password';
CREATE SCHEMA catalog AUTHORIZATION catalog_sync;
SQL
