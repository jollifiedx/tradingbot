/**
 * Latest account equity snapshot (GET /account). The dashboard API never
 * talks to Webull (Invariant 2) -- these figures come from the worker's own
 * reconciliation snapshots in the DB, not a live broker call. 404 is
 * expected until the worker has run at least once; render that state
 * plainly rather than as an error.
 */
import { useAccountQuery } from "../hooks/useAccount";
import { formatMoney } from "../lib/money";
import { formatLocalDateTime } from "../lib/time";

export function AccountPanel() {
  const { data, isLoading, isError, error } = useAccountQuery();

  return (
    <section className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-700">
      <h2 className="mb-3 text-base font-semibold text-neutral-900 dark:text-neutral-50">
        Account
      </h2>

      {isLoading && <p className="text-sm text-neutral-500">Loading account…</p>}
      {isError && (
        <p className="text-sm text-red-600" role="alert">
          Could not load account snapshot: {error.message}
        </p>
      )}
      {data === null && (
        <p className="text-sm text-neutral-500">
          No account snapshot yet -- the worker hasn't recorded one. This is
          expected until it has run at least once.
        </p>
      )}

      {data && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat label="Equity" value={formatMoney(data.account_equity)} />
          <Stat label="Cash" value={formatMoney(data.cash_balance)} />
          <Stat label="Buying power" value={formatMoney(data.buying_power)} />
          {data.spy_benchmark_equity && (
            <Stat
              label="SPY benchmark equity"
              value={formatMoney(data.spy_benchmark_equity)}
            />
          )}
          <Stat label="Environment" value={data.is_paper ? "Paper" : "LIVE"} />
          <Stat
            label="As of"
            value={formatLocalDateTime(data.recorded_at)}
            wide
          />
        </div>
      )}
    </section>
  );
}

function Stat({
  label,
  value,
  wide,
}: {
  label: string;
  value: string;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "col-span-full" : undefined}>
      <p className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </p>
      <p className="text-lg font-semibold text-neutral-900 dark:text-neutral-50">
        {value}
      </p>
    </div>
  );
}
