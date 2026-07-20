---
name: api-client-regen
description: Regenerate the typed TS client from FastAPI's OpenAPI spec after any backend route or model change. Use whenever backend API contracts change or frontend types look stale.
---

Regenerate the typed API client:

1. Export the current spec from the local FastAPI app (running dev server `/openapi.json`,
   or the offline export script if present).
2. Run `openapi-typescript` to regenerate `frontend/src/api/` (path per frontend README).
3. Run `tsc --noEmit` in `frontend/`. Compile errors = the contract changed in a breaking
   way: fix the CONSUMERS to match the new contract — never hand-edit the generated files,
   never patch types to silence errors.
4. Report what changed in the generated surface (new/removed endpoints, changed shapes).

This also runs in CI as a drift check: regenerated output differing from the committed
client fails the build.
