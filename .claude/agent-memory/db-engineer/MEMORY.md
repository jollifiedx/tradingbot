# db-engineer memory index

- [Initial schema design decisions](project_initial_schema.md) — FK cycle resolution, append-only trigger pattern, trades' custom guard, app_owner RLS pattern, pgvector HNSW choice
- [File-only DB work mode](feedback_file_only_db_work.md) — when no Supabase project is linked, write migrations only, never apply/MCP/CLI
- [Pydantic models mirroring schema](project_pydantic_models.md) — BotSettings naming, AwareDatetime, which models are frozen and why, per-column (not per-table) numeric fidelity, enum/SQL drift test
