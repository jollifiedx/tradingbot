---
name: deploy
description: Deploy api, frontend, or worker with the market-hours gate and post-deploy health verification. Owner-triggered only.
disable-model-invocation: true
argument-hint: [api|frontend|worker|all] [dev|paper|live]
---

Deploy $ARGUMENTS.

Pre-flight (all must pass before anything ships):
1. CI green on the commit being deployed (including the safety suite).
2. **Market-hours gate for the worker:** check exchange_calendars for the current NYSE
   session — if it is 9:30-16:00 ET on a trading day, REFUSE to deploy the worker and say
   when the window opens. No override exists in this skill.
3. Confirm target environment: paper and live are separate Railway environments.
   ANY deploy touching the LIVE environment requires Esther to explicitly confirm the
   word "live" in this conversation — a `/deploy worker to live` invocation alone is not
   sufficient confirmation.

Deploy: frontend → Vercel; api/worker → Railway (respective environment). Migrations run
via Supabase CLI before the api/worker restart.

Post-deploy verification (mandatory): api health endpoint OK; worker starts, completes
startup reconciliation, and reports HALTED-until-verified → healthy; frontend loads and
the generated-client version matches the deployed API. Log the deploy (commit, target,
time, verifier output) to `docs/deploys.md`.
