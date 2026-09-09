#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres --set=app_password="$POSTGRES_PASSWORD" --set=read_password="$POSTGRES_READ_PASSWORD" <<'SQL'
CREATE USER openproject PASSWORD :'app_password';
CREATE USER wikijs PASSWORD :'app_password';
CREATE USER mattermost PASSWORD :'app_password';
CREATE DATABASE openproject OWNER openproject;
CREATE DATABASE wikijs OWNER wikijs;
CREATE DATABASE mattermost OWNER mattermost;
CREATE USER infraaxon_reader PASSWORD :'read_password';
GRANT pg_monitor TO infraaxon_reader;
\connect openproject
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA pg_catalog;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA pg_catalog;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA pg_catalog;
SQL
