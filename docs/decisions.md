# Decision Log

One dated paragraph per significant decision: what, alternatives, why.

## 2026-07-19 — Repo scaffolded in place (OneDrive), build artifacts excluded

Kept the repository at its current OneDrive location per owner preference rather than
moving code outside sync. Mitigation: `.gitignore` excludes `node_modules/`, `.venv/`,
caches, and build output so OneDrive never syncs dependency trees. Revisit only if sync
contention actually bites.

## 2026-07-19 — Initial audit-table shapes APPROVED by owner

Esther approved the initial schema as written (migrations 000001–000011), including the
three escalated decisions: settings born frozen with caps=0.00, the decisions.action enum,
and orders status/type enums with previous_order_id chaining for transitions. Approval
covers the shape as of commit 6edfa0e; any future change to these tables re-triggers the
owner-approval requirement per CLAUDE.md.

## 2026-07-21 — Reconciliation: a partial verification is NOT a verification

Reconciliation can compare positions but not cash, because no independent DB cash
expectation exists until the order path records trades (comparing the broker's cash to our
copy of the broker's cash proves nothing). Architect flagged that a positions-only run still
reported `reconciled=True`, and the safety gate consumes a bare bool — so a half-check read
as a full check one call frame away (same shape as the earlier NaN fail-open).

Esther ruled: **partial isn't verified.** A run that did not compare cash reports status
`CASH_NOT_VERIFIED` and `reconciled=False`, so it cannot permit trading. Implemented
structurally, not by convention: `reconciled` is a derived property (`status is CLEAN`), and
`__post_init__` rejects every hand-built inconsistent combination. Positions detail is kept
separately (`positions_reconciled`, `cash_checked`) for diagnosis only. Alternatives rejected:
relying on the wiring PR to check `cash_checked` (guarantee lives in caller discipline — the
exact pattern that produced the finding), and accepting positions-only for paper trading.
Costs nothing today (no order path exists) and lifts automatically when the cash ledger lands.

## 2026-07-21 — Reconciliation halts: real drift is sticky, blips may auto-clear

Invariant 6 says never silently *fix* a mismatch; architect noted that silently *recovering*
is the same hazard from the other end — a stateless reconcile means run N halts on drift and
run N+1 returns clean, re-enabling trading with no human acknowledgement.

Esther ruled: **real drift latches, transient failures may auto-clear.** Every status maps to
exactly one `HaltCategory`: DRIFT (unexpected/missing position, quantity mismatch, duplicate,
cash mismatch) is sticky — the worker stays halted until the owner clears it via the freeze
flag, and a transient headline cannot un-latch a run that observed drift. TRANSIENT
(broker/DB unreadable, account not pinned) may clear on a later clean run. NOT_VERIFIED (the
cash gap) is neither — it resolves structurally, not by owner action. Rejected: manual clear
for everything (network blips would wake the owner), and auto-resume (a real drift could
clear itself). The module is stateless and cannot enforce the latch — the scheduler that runs
reconciliation periodically owns it, and its PR needs a test proving DRIFT-then-CLEAN does
not re-enable trading.

## 2026-07-19 — Frontend deferred until first API routes exist

Frontend build starts after `GET /positions`, `GET /decisions`, `GET/PATCH /settings`
exist, because the entire frontend data layer is generated from the FastAPI OpenAPI spec.
Building UI against invented mocks was rejected as guaranteed rework. Critical path
instead: Webull OpenAPI application (1–2 day approval) → DB schema → broker wrapper →
paper harness.
