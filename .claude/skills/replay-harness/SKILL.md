---
name: replay-harness
description: Deterministic market-replay testing — feed recorded tick/bar data through the worker at accelerated speed to verify end-to-end behavior. Use for regression testing and reproducing "what did the bot see at time X".
argument-hint: [date/session to replay, what to verify]
---

Replay task: $ARGUMENTS

- Replays recorded market data (captured from paper sessions) through the REAL worker code
  path — scheduler, rules engine, safety gate, paper-order stubs — at accelerated speed.
  Same code, canned inputs, deterministic outputs.
- Time control via injected clock (the worker never reads wall-clock directly precisely so
  replay works). exchange_calendars still governs the simulated session.
- Uses: regression fixtures (recorded day + expected decisions committed together);
  reproducing incidents ("what did the bot see at 10:42 ET?"); verifying halts fire at the
  exact tick they should.
- Order submissions in replay go to an in-memory stub that records intents — never to
  Webull, not even paper. Assert on the recorded intents.
- A replay run's output must be byte-stable across runs; nondeterminism in a replay is a
  bug in the worker (hidden I/O, unseeded randomness, wall-clock leak) — find it.
