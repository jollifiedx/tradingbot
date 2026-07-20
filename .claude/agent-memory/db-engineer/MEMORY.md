# db-engineer memory index

- [Initial schema design decisions](project_initial_schema.md) — FK cycle resolution, append-only trigger pattern, trades' custom guard, app_owner RLS pattern, pgvector HNSW choice
- [File-only DB work mode](feedback_file_only_db_work.md) — when no Supabase project is linked, write migrations only, never apply/MCP/CLI
