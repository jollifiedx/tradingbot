---
name: ci-pipeline
description: Maintain GitHub Actions — lint, typecheck, safety suite as required check, client-drift check, backup cron, gated deploys. Use for any CI/CD workflow change.
argument-hint: [pipeline change]
---

CI task: $ARGUMENTS

Pipeline contract (.github/workflows/):
- **PR checks (all required):** `ruff check` + `ruff format --check`; `mypy --strict`;
  `pytest` with the safety suite explicitly included — the safety-test job is a REQUIRED
  status check and must never be made optional or skippable; `tsc --noEmit`;
  generated-client drift check (regenerate, diff against committed, fail on difference).
- **Nightly cron:** `pg_dump` backup to private storage (free-tier substitute for Supabase
  Pro backups).
- **Deploy jobs:** migrations via Supabase CLI first; worker deploy gated by an
  exchange_calendars market-hours check (never a hardcoded schedule); worker jobs
  triggered manually or after close, never on-merge auto-deploy.
- Secrets only as GitHub Actions secrets / Railway env vars; workflow logs must never
  echo them.
- Coverage on `backend/app/worker/` safety paths is reported and must not decrease.
