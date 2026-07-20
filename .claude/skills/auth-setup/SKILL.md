---
name: auth-setup
description: Implement or modify the auth chain — Supabase Auth with mandatory TOTP 2FA, single-user ID allowlist, FastAPI JWT verification middleware. Use for any authentication or authorization work.
argument-hint: [auth task]
---

Auth task: $ARGUMENTS

Architecture (treat this UI like a bank login — it can move money):
- Supabase Auth, email + password + MANDATORY TOTP 2FA (supabase.com/docs/guides/auth/auth-mfa).
- Single-user system: a server-side allowlist of exactly Esther's user ID, checked in
  FastAPI middleware on EVERY route — a valid JWT for any other user is rejected with 403.
- FastAPI verifies the Supabase JWT signature and expiry on every request; no route ships
  unauthenticated except health checks.
- Frontend: Supabase session handling; `VITE_SUPABASE_ANON_KEY` only — service-role key
  never leaves the backend, broker/Anthropic keys never touch auth or frontend code.

Every auth change ships with tests proving: expired JWT rejected, wrong-user JWT rejected,
missing token rejected, valid owner token accepted. Auth changes are on the ask-first list
in CLAUDE.md — get approval before altering the auth model itself.
