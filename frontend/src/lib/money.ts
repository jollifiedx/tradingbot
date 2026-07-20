/**
 * Money helpers.
 *
 * The API sends every money field as a JSON string (Decimal-safe, per
 * CLAUDE.md). This module is DISPLAY-ONLY: `formatMoney` parses the string
 * purely to produce a human-readable string (thousands separators, currency
 * symbol) and returns a new string -- the numeric result is never stored,
 * submitted, or used in further arithmetic anywhere in the app. Every form
 * that accepts money keeps the raw string value end-to-end (see
 * `src/components/CapsEditor.tsx`) and sends that same string back to the
 * API untouched. Do not add a helper here that returns a `number` for
 * anything other than throwaway display formatting.
 */

export function formatMoney(value: string): string {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return value;
  }
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
