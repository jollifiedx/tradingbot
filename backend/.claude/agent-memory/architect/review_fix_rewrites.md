---
name: review-fix-rewrites
description: Re-reviewing a fix — diff the rewritten module against the version you reviewed, because untested defenses disappear silently and docstrings start describing the refactor that was abandoned
metadata:
  type: feedback
---

When a finding is closed by *rewriting* a module (rather than a surgical patch), two failures
recur. Check both before signing off, and never re-review only the new tests.

**1. A defense with no test vanishes and nothing goes red.** Re-reviewing `app/worker/scheduler.py`
(2026-07-22, commit 4217677): the fix for the stale-verdict blocker replaced `fresh_result`'s body
with a shared `_within()` helper and, in doing so, dropped the previous version's explicit
naive-datetime guard (`if now.tzinfo is None: return None`, with a comment explaining it). No test
covered it, so 385 tests stayed green. Technique: `git show <old>:<path>` (or the pre-commit working
tree) and grep the OLD file for its guard clauses — `tzinfo`, `is None`, `is not True`,
`math.isfinite` — then confirm each still exists or was deliberately replaced.

**2. The docstring describes the refactor that was not done.** The same commit added a
"reconciliation runs outside the state lock, so two runs can finish out of order" paragraph to
`record_reconciliation` — but `run_reconcile` still holds the lock across the await. The
recommendation was written into the prose and dropped in the code. Technique: for every mechanism
a docstring *asserts* (ordering, locking, freshness, "never raises"), find the line that implements
it before crediting it.

**Why:** Both were found by probing rather than reading, and both were introduced by an otherwise
excellent fix — competence in the fix says nothing about what the rewrite took with it.

**How to apply:** Re-review = run the ORIGINAL probes against the new code (they are cheap and they
are the only thing that proves the finding is closed), plus a guard-clause diff of old vs new. See
[[drift-stale-verdict]] for the finding this came out of and [[review-ast-tripwires]] for the
companion "verify the predicate, not the claim" technique.
