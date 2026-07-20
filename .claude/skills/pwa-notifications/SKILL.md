---
name: pwa-notifications
description: PWA installability and push notifications for fills, halts, and errors. Use for service worker, push subscription, or notification trigger work.
argument-hint: [notification task]
---

PWA/notification task: $ARGUMENTS

The requirement: Esther is at work; she must never be surprised by what the bot did.
Every fill, every halt, every error → push notification.

- PWA via vite-plugin-pwa: installable, service worker, web app manifest.
- Push: Web Push API; subscription stored in Supabase; notification triggers fire from the
  worker via a Supabase Edge Function (worker → DB event → function → push), so the
  worker never blocks on notification delivery.
- Event tiers: HALT/ERROR (maximum urgency, distinct style — these mean "look now");
  FILL (informational); DAILY SUMMARY (quiet). Per-tier toggle in settings, except
  HALT/ERROR which cannot be disabled.
- Notification payloads contain no secrets and no account balance — symbol, action, and a
  deep link into the dashboard.
- Test path: a dev-only "send test notification" trigger; never test against the live
  worker's event stream.
