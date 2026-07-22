---
name: review-ast-tripwires
description: How to assess AST-based "forbidden pattern" tests in this repo — run their predicate against bypass shapes before crediting them as a guard
metadata:
  type: feedback
---

This project's safety modules ship AST tripwire tests (parse the module, assert a name never
appears as an attribute / a call / inside an `if` test). Treat them as regression pins for a
*verbatim* recurrence, never as the primary guard, and always verify the predicate rather than
the docstring.

**Why:** Reviewing `app/worker/scheduler.py` (2026-07-21) I re-ran the three AST predicates from
`tests/test_worker_scheduler.py` against a snippet containing the banned behaviours in other
shapes; all three found nothing: `getattr(result, "reconciled")`; branching on a diagnostic that
is not in the forbidden set (`result.status`, `result.mismatches`); `ok = decision.reason is
CLEAR` then `if ok:`; `match`/`while` instead of `if`; an aliased receiver (`db = self._db`).
Separately, mypy DOES catch aliased protocol misuse but not `getattr`, so the layers fail
together on exactly one shape.

**How to apply:**
- Cheap verification: copy the test's predicate into a scratch script, run it over a snippet of
  the bypasses, and report what it misses — concrete, and it takes a minute.
- Rank the layers in the report: type-level narrowing (a two-method Protocol) and DB triggers are
  load-bearing; behavioural tests ("every tick issues one more read") are next; AST tests are last.
- If a forbidden-name set is used, check it is closed over the hazard: banning `reconciled` while
  leaving `status`/`category`/`mismatches` readable does not stop the scheduler re-deriving the
  latch's job from diagnostics.
- Pairs with [[drift-caller-discipline]]: a tripwire is a mechanism only where it is exhaustive.
