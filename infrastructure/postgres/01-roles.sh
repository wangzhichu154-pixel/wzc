#!/bin/sh
set -eu
# Passwords are psql variables, never interpolated as SQL syntax.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
    --set=migrator_password="$DB_MIGRATOR_PASSWORD" \
    --set=app_password="$DB_APP_PASSWORD" \
    --set=monitor_password="$DB_MONITOR_PASSWORD" <<'SQL'
CREATE ROLE trading_migrator LOGIN PASSWORD :'migrator_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE trading_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE trading_monitor LOGIN PASSWORD :'monitor_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT pg_monitor TO trading_monitor;
ALTER DATABASE trading OWNER TO trading_migrator;
REVOKE ALL ON DATABASE trading FROM PUBLIC;
GRANT CONNECT ON DATABASE trading TO trading_app, trading_monitor;
ALTER SCHEMA public OWNER TO trading_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO trading_app;
ALTER DEFAULT PRIVILEGES FOR ROLE trading_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO trading_app;
ALTER DEFAULT PRIVILEGES FOR ROLE trading_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO trading_app;
SQL
