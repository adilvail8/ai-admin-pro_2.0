#!/bin/sh
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
DO
\$\$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles WHERE rolname = '${APP_DB_USER}'
    ) THEN
        CREATE ROLE "${APP_DB_USER}" LOGIN PASSWORD '${APP_DB_PASSWORD}';
    END IF;
END
\$\$;

SELECT 'CREATE DATABASE "${APP_DB_NAME}" OWNER "${APP_DB_USER}"'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = '${APP_DB_NAME}'
)\gexec

GRANT ALL PRIVILEGES ON DATABASE "${APP_DB_NAME}" TO "${APP_DB_USER}";
EOSQL
