---
name: ui-component
description: Generate dashboard components following project conventions — shadcn/ui, TanStack Query on the generated API client, Zod forms. Use for building or modifying React components in frontend/.
argument-hint: [component spec]
---

Component task: $ARGUMENTS

Conventions (strict):
- Data: ALL backend data via the generated TS client in `frontend/src/api/` wrapped in
  TanStack Query hooks — a hand-written fetch is a defect. Live updates via Supabase
  Realtime subscriptions invalidating the relevant query keys.
- State: server state in TanStack Query; Zustand ONLY for UI state (toggles, chart prefs).
  Control state (frozen, caps) lives in the DB — the UI reads and mutates, never owns.
- Forms: React Hook Form + Zod; share Zod schemas with API payload validation.
- Styling: Tailwind + shadcn/ui primitives; PascalCase component files; strict TS must
  pass (`tsc --noEmit`).
- Money renders from string/Decimal-safe values — no float math in the UI. Times display
  local, payloads stay UTC.
- Fat-finger safety: freeze/unfreeze and cap changes get explicit confirmation steps;
  freeze state is visible on every screen. Primary user is on a phone — mobile-first.
