# Execution-Guardian Memory Index

- [Money request-body schema must be string](money_request_schema_string.md) — force Decimal request fields to OpenAPI `string`, not `number`, for TS Decimal-safety
- [Permanent halt is correct today](permanent_halt_is_correct_today.md) — `may_trade=True` is unreachable until a DB cash ledger exists; do not "fix" it
- [Safety writes must be confirmed](freeze_write_must_be_confirmed.md) — judge a persisted halt on the returned row, and refuse to trade until the retry lands
- [Run checks with the backend venv](backend_venv_interpreter.md) — bare `python` is a different interpreter without the deps
