-- Extensions required by the TradingBot schema.
-- Must run before any table that depends on them (pgvector's `vector` type,
-- pgcrypto's gen_random_uuid()).

create extension if not exists pgcrypto;
create extension if not exists vector;
