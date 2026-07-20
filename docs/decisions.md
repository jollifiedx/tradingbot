# Decision Log

One dated paragraph per significant decision: what, alternatives, why.

## 2026-07-19 — Repo scaffolded in place (OneDrive), build artifacts excluded

Kept the repository at its current OneDrive location per owner preference rather than
moving code outside sync. Mitigation: `.gitignore` excludes `node_modules/`, `.venv/`,
caches, and build output so OneDrive never syncs dependency trees. Revisit only if sync
contention actually bites.

## 2026-07-19 — Frontend deferred until first API routes exist

Frontend build starts after `GET /positions`, `GET /decisions`, `GET/PATCH /settings`
exist, because the entire frontend data layer is generated from the FastAPI OpenAPI spec.
Building UI against invented mocks was rejected as guaranteed rework. Critical path
instead: Webull OpenAPI application (1–2 day approval) → DB schema → broker wrapper →
paper harness.
