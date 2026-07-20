---
name: money-request-schema-string
description: Pydantic v2 renders Decimal request-body fields as number|string|null; force string in OpenAPI so the generated TS client stays Decimal-safe
metadata:
  type: feedback
---

For any FastAPI **request body** with a money `Decimal` field, the default Pydantic v2
validation JSON Schema is `anyOf: [{number, minimum}, {string, pattern}, {null}]` — i.e. it
advertises `number`, which makes `openapi-typescript` generate a JS `number` and lose money
precision.

Fix: annotate the input money type with `WithJsonSchema({"type": "string"}, mode="validation")`
so only `string` is emitted. This changes the emitted schema only; the underlying Decimal core
schema still parses/enforces `max_digits`/`decimal_places`/`ge` at runtime. Response bodies
(`response_model=BotSettings`) already serialize money as string via the serialization schema —
this gotcha is request-only.

**Why:** money must be `Decimal`/string end-to-end (CLAUDE.md convention: money never float). The
brief for `PATCH /settings` explicitly required request-body money typed string, not number.
**How to apply:** reuse the `MoneyPatchInput` pattern in `app/api/routers/settings.py` for any new
POST/PATCH route that accepts caps/prices/amounts. See [[patch-settings-route]].
