-- Runs once, on first creation of the pgdata volume. Editing this file later has
-- no effect until `docker compose down -v` throws the volume away.

-- Trigram index support for the ticker/company name search in Epic 3.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Dedicated database for pytest, so a test run truncating tables can never
-- touch the data you have been ingesting into `whalewatch` all afternoon.
CREATE DATABASE whalewatch_test;

-- psql meta-command: extensions are per-database, so the test DB needs its own.
\connect whalewatch_test
CREATE EXTENSION IF NOT EXISTS pg_trgm;
