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

## 2026-07-21 — Strategy: hybrid, built swing-first, as strategies inside ONE worker

The parked strategy-timeframe decision, now ruled. Esther wants a HYBRID (daily bars pick
what to hold, intraday bars refine when to enter), but was rightly worried about complexity
and testing time landing all at once.

Ruling, two parts:
1. **Incremental, not big-bang.** Build the SWING layer first (daily/hourly bars, hold
   days–weeks): simpler, fewer trades, far less cost drag, easiest to prove vs SPY, and it
   fits an owner who is at work all day. Prove it in forward paper trading FIRST. Only then
   add the intraday entry-timing layer as a separate, self-contained increment. The swing
   layer is literally the first half of the hybrid — nothing is thrown away. Bar timeframe and
   holding period remain independent choices within this.
2. **Separate STRATEGIES, not separate BOTS.** Esther asked about running two worker
   processes (one swing, one intraday). Rejected: they would share ONE Webull account, one
   cash pool, and one set of ACCOUNT-LEVEL limits (buy-power cap, daily-loss, per-trade), so
   two independent processes would race each other on the cap and on order placement, and
   reconciliation (which assumes ONE writer of intent) could not attribute a shared-symbol
   position to a bot. It would also duplicate the entire safety spine (scheduler, latch,
   freeze) — more to test, not less. Instead: ONE worker owns all shared safety-critical
   machinery (account, caps, reconciliation, order path, the single freeze kill-switch), and
   the rules engine runs multiple STRATEGY modules (swing now, intraday later) that are each
   independently testable and all feed orders through the SAME safety gate. One car, one set
   of brakes, two playbooks — not two drivers on one wheel.

Consequence for the build: the rules engine is designed for pluggable strategy modules from
the start, but only the swing module is built first. The safety gate / order path / worker are
strategy-agnostic and shared.

## 2026-07-21 — The worker MAY set the freeze flag (one-way), with DB-level guardrails

To survive a restart, a drift halt must persist. The architect flagged that the planned
mechanism — the worker writing `settings.frozen = true` — needs owner approval: it is on
CLAUDE.md's "never without explicit owner approval" list, and it inverts Invariant 2's stated
flow ("UI mutates settings; worker reads settings"). The 2026-07-21 latch ruling said the
owner *clears* the freeze but never said who *sets* it.

Esther ruled: **the worker may set the freeze flag, one-way, with guardrails.** Invariant 2 is
amended in exactly one direction: the worker may ENGAGE the freeze, never release it.
Guardrails, both mechanism not convention:
1. A DB trigger rejects any `frozen` true→false transition attributed to a NULL (system)
   actor. The worker's only write path hardcodes `frozen=true, updated_by=NULL`, so the
   database itself refuses a worker-initiated unfreeze — not just the application code.
2. System halts are attributed to NULL, never to Esther's UID. `settings.updated_by` is a FK
   to `auth.users` and the worker is not a user; faking her identity would corrupt
   `settings_history`, the very record a postmortem depends on. Convention going forward:
   `changed_by IS NULL` = the bot halted itself, non-NULL = Esther acted.

Rejected: a separate append-only `halts` table (keeps Invariant 2 literally intact, but
creates a second source of "am I halted?" that the safety gate, API and UI must all learn to
read — every one that forgets fails open); and app-code convention alone (the caller-discipline
pattern already rejected on 2026-07-21).

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
