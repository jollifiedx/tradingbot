---
name: incident-triage
description: Investigate a halt, error, or unexpected bot behavior — build a timeline from logs and the decision log, identify the trigger, recommend a fix. Use when the bot halted or did something surprising.
argument-hint: [what happened / time window]
---

Triage: $ARGUMENTS

Method (evidence only, no speculation past it):
1. Pull the halt/error context: Railway worker logs for the window + the halt reason enum
   and its triggering values.
2. Reconstruct the timeline by joining logs (client_order_id / decision_id) with
   `decisions`, `orders`, and `trades` rows. Quote actual rows and log lines.
3. Identify the trigger and classify: external (broker outage, data gap), internal bug,
   or safety system working as designed (a correct halt is a SUCCESS — say so plainly).
4. Recommend — never auto-apply — next steps: which agent/skill owns the fix, what
   evidence they need, and whether it is safe to unfreeze (that decision is Esther's).

If the evidence shows a SAFETY-SYSTEM FAILURE (trade while frozen, cap breach, trade on
stale data): stop everything, state it first and unambiguously — this is the project's
zero-tolerance event and the finding Esther must see before anything else.
