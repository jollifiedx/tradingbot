---
name: db-backup-restore
description: Run or verify the pg_dump backup and perform a test restore into a scratch database. The decision log is the project's most valuable artifact.
disable-model-invocation: true
argument-hint: [backup | test-restore]
---

Backup/restore task: $ARGUMENTS

**Backup:** run `pg_dump` against `DATABASE_URL` (from env, never echoed), producing a
timestamped compressed dump. Verify the artifact is non-empty and lists the expected tables
(`pg_restore --list`). Confirm the nightly GitHub Actions cron ran within the last 24h.

**Test restore:** restore the latest dump into a scratch database (local Supabase or a
throwaway schema — NEVER the dev DB in place, NEVER production). Verify: row counts for
`decisions`, `orders`, `trades` match the source; append-only triggers/policies survived
the restore; a sample pgvector similarity query works.

Report the backup age, size, and restore-test result. If the latest backup is missing or
stale, treat it as an incident to flag, not a silent re-run.
