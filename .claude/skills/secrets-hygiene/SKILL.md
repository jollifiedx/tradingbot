---
name: secrets-hygiene
description: Rules for handling Webull, Anthropic, and Supabase credentials — where they live, environment separation, rotation, and leak response. Background knowledge for config and deployment work.
user-invocable: false
---

Standing rules for credentials:

- Secrets exist ONLY in Railway env vars (deployed) or local `.env` (gitignored). Never in
  code, git, logs, reports, or chat output. Env var names are documented in CLAUDE.md;
  values are never written anywhere else.
- Paper and live are SEPARATE Railway environments with SEPARATE Webull App Key/Secret
  pairs. Code selects via `WEBULL_ENV` (paper|live); nothing may read live credentials in
  the paper environment or vice versa.
- The Supabase service-role key exists only in backend env; frontend gets anon key only.
- Rotation order on suspected leak: 1) freeze the bot (set frozen flag), 2) rotate the
  leaked key at the provider, 3) update Railway env, 4) verify worker reconnects and
  reconciles, 5) unfreeze. Never rotate-first — a live bot with dead credentials fails
  closed, which is correct, but a leaked key with a running bot is the emergency.
- A secret value appearing in any file, log, or output is an incident: stop work and
  surface it immediately.
