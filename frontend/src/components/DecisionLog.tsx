/**
 * The append-only decision audit log (GET /decisions), newest-first (the
 * backend already orders by `decided_at desc`). Read-only -- `decisions` has
 * no write path from the dashboard (Invariant 5). Times render in local
 * zone from UTC payloads (`formatLocalDateTime`); conviction is a
 * Decimal-safe string, never parsed to a float for display math (only
 * rendered as text).
 */
import { useDecisionsQuery } from "../hooks/useDecisions";
import { formatLocalDateTime } from "../lib/time";

const ACTION_STYLES: Record<string, string> = {
  buy: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
  sell: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
  hold: "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
  no_trade: "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
};

export function DecisionLog() {
  const { data, isLoading, isError, error } = useDecisionsQuery();

  return (
    <section className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-700">
      <h2 className="mb-3 text-base font-semibold text-neutral-900 dark:text-neutral-50">
        Decision log
      </h2>

      {isLoading && (
        <p className="text-sm text-neutral-500">Loading decisions…</p>
      )}
      {isError && (
        <p className="text-sm text-red-600" role="alert">
          Could not load decisions: {error.message}
        </p>
      )}
      {data && data.length === 0 && (
        <p className="text-sm text-neutral-500">No decisions recorded yet.</p>
      )}

      {data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[480px] text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 text-neutral-500 dark:border-neutral-700">
                <th className="py-2 pr-3 font-medium">Time</th>
                <th className="py-2 pr-3 font-medium">Symbol</th>
                <th className="py-2 pr-3 font-medium">Action</th>
                <th className="py-2 pr-3 font-medium">Conviction</th>
                <th className="py-2 font-medium">Rationale</th>
              </tr>
            </thead>
            <tbody>
              {data.map((d) => (
                <tr
                  key={d.id}
                  className="border-b border-neutral-100 align-top dark:border-neutral-800"
                >
                  <td className="whitespace-nowrap py-2 pr-3 text-neutral-500">
                    {formatLocalDateTime(d.decided_at)}
                  </td>
                  <td className="py-2 pr-3 font-medium">{d.symbol}</td>
                  <td className="py-2 pr-3">
                    <span
                      className={
                        "rounded px-2 py-0.5 text-xs font-semibold uppercase " +
                        (ACTION_STYLES[d.action] ?? ACTION_STYLES.no_trade)
                      }
                    >
                      {d.action.replace("_", " ")}
                    </span>
                  </td>
                  <td className="py-2 pr-3 text-neutral-500">
                    {d.conviction ?? "—"}
                  </td>
                  <td className="max-w-xs py-2 text-neutral-600 dark:text-neutral-300">
                    {d.llm_rationale ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
