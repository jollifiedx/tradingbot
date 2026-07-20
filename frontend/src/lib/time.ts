/**
 * Time helpers. All API payloads are UTC ISO-8601 timestamps (`timestamptz`
 * columns) -- this module's only job is to render them in Esther's local
 * timezone for display. Nothing here mutates or re-serializes a timestamp
 * for a request body; requests never need to send a timestamp.
 */

export function formatLocalDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

export function formatLocalTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toLocaleTimeString(undefined, { timeStyle: "medium" });
}
