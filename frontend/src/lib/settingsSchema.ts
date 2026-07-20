import { z } from "zod";

/**
 * Mirrors the backend's `numeric(14,2)` CHECK (`value >= 0`) constraint on
 * `buy_power_cap` / `max_daily_loss` / `max_per_trade_cap` (see
 * backend/app/api/routers/settings.py, backend/app/core/models.py). Pure
 * regex validation -- no `parseFloat`/`Number()` is used to decide validity,
 * so this never does float math on money (CLAUDE.md: money renders from
 * string/Decimal-safe values, never float-math in the UI). The no-leading-
 * minus-sign rule in the pattern is what enforces "non-negative", not a
 * numeric comparison.
 */
export const moneyStringSchema = z
  .string()
  .trim()
  .min(1, "required")
  .regex(
    /^\d+(\.\d{1,2})?$/,
    "enter a non-negative amount, up to 2 decimal places (e.g. 1000.00)",
  );

export const capsFormSchema = z.object({
  buy_power_cap: moneyStringSchema,
  max_daily_loss: moneyStringSchema,
  max_per_trade_cap: moneyStringSchema,
  staleness_threshold_seconds: z
    .number({ error: "must be a whole number of seconds" })
    .int("must be a whole number")
    .positive("must be greater than zero"),
});

export type CapsFormValues = z.infer<typeof capsFormSchema>;
