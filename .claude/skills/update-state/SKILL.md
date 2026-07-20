---
name: update-state
description: End-of-session bookkeeping — update the Current State section of CLAUDE.md and append significant decisions to the decision log. Use at the end of a work session or after completing a milestone.
---

Update project state:

1. Review what this session actually accomplished (conversation + `git status`/diff if a
   repo exists). Verified work only — nothing aspirational.
2. Update the **Current State** section of `.claude/CLAUDE.md`:
   - **Built:** add completed, verified work.
   - **In progress:** what is genuinely mid-flight, with enough context to resume cold.
   - **Known issues / debt:** new debt discovered or incurred; remove items actually
     resolved.
   Keep the section tight — facts, not narrative. Do not touch other CLAUDE.md sections.
3. If a significant decision was made this session (architecture, scope, tooling,
   risk posture), append one dated entry to `docs/decisions.md` (create if missing):
   decision, alternatives considered, why. One paragraph maximum.
4. Report the diff of what changed in both files.
