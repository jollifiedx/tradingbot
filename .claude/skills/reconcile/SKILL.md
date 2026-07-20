---
name: reconcile
description: Build or debug the reconciliation job — compare Webull truth (positions/cash) against DB intent; halt and alert on mismatch. Use for any reconciliation or state-drift work.
argument-hint: [reconciliation task]
---

Reconciliation task: $ARGUMENTS

Invariant 6: Webull is the source of truth for positions/cash; the DB is the source of
truth for intent/reasoning. Reconciliation compares them and on ANY mismatch → halt +
alert. NEVER silent-fix, never "adjust the DB to match."

- Runs at worker startup (mandatory — worker stays halted until first reconcile passes)
  and periodically during market hours.
- Compares: open positions (symbol, qty, side), cash balance, open orders (by client
  order ID), and any fills recorded broker-side but missing DB-side.
- Drift scenarios tests must cover: manual out-of-band trade in the Webull app, missed
  fill event during worker downtime, crash between order-persist and submission, partial
  fill recorded on one side only, duplicate client order ID.
- A mismatch report states exactly what differs, both values, and which invariant applies.
  Resolution is a human decision (or an explicit documented rule) — never improvised.
