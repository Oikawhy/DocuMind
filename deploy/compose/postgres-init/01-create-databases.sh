#!/usr/bin/env sh
set -eu

for database_name in temporal temporal_visibility langfuse; do
    if ! psql --username "$POSTGRES_USER" --dbname postgres --tuples-only --no-align \
        --command "SELECT 1 FROM pg_database WHERE datname = '$database_name'" | grep -q 1; then
        psql --username "$POSTGRES_USER" --dbname postgres --command "CREATE DATABASE $database_name"
    fi
done
