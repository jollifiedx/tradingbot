---
name: market-data-stream
description: MQTT quote streaming, gRPC order events, staleness heartbeat, reconnect logic — the dead-man's switch. Use for any real-time market data or stream-reliability work.
argument-hint: [streaming task]
---

Streaming task: $ARGUMENTS

This is the scariest failure surface in the system: a worker trading on stale quotes.
Non-negotiables:

- Every tick updates a per-symbol heartbeat timestamp. No tick for N seconds (N read from
  `settings`) → raise the staleness flag; the worker halts NEW entries while it is set.
  This is Architecture Invariant 3 (fail closed).
- Reconnect with capped exponential backoff + jitter. After ANY reconnect, fetch a REST
  snapshot and re-sync before trusting the stream again — never resume on stream data
  alone.
- gRPC order-event subscription feeds fill/reject events to the execution layer; a dropped
  event subscription is treated the same as stale data (halt-new-entries).
- Tests must cover: silent stream (connected but no data), mid-tick disconnect, reconnect
  storm, out-of-order events, clock skew between tick timestamp and local time.
- Use asyncio; no thread-per-symbol cleverness. Prefer boring.
