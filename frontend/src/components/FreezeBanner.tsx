/**
 * The single most important element on the screen: unmistakable at-a-glance
 * trading status. Esther is usually glancing at this on her phone, at work
 * -- it must be readable in under a second. Visible on every screen (the
 * only screens that exist right now are Login and the Dashboard; Dashboard
 * always renders this at the top).
 *
 * States:
 *  - loading: neutral, "checking status"
 *  - error: amber "STATUS UNKNOWN" -- we could not confirm freeze state.
 *    This is a display-only concern (the UI never enforces the freeze
 *    itself, the worker does per Invariant 2/3), but showing green/red on
 *    unconfirmed data would be worse than useless -- it would be actively
 *    misleading. Fail closed in what we SHOW, too.
 *  - frozen === true: red "FROZEN"
 *  - frozen === false: green "ACTIVE"
 */
import { useSettingsQuery } from "../hooks/useSettings";

export function FreezeBanner() {
  const { data, isLoading, isError } = useSettingsQuery();

  if (isLoading) {
    return (
      <div className="w-full bg-neutral-300 px-4 py-3 text-center text-sm font-semibold text-neutral-700 dark:bg-neutral-700 dark:text-neutral-200">
        Checking bot status…
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div
        className="w-full bg-amber-500 px-4 py-3 text-center text-base font-bold text-white"
        role="status"
      >
        STATUS UNKNOWN — could not confirm freeze state
      </div>
    );
  }

  if (data.frozen) {
    return (
      <div
        className="w-full bg-red-600 px-4 py-4 text-center text-2xl font-extrabold tracking-wide text-white"
        role="status"
      >
        FROZEN — not trading
      </div>
    );
  }

  return (
    <div
      className="w-full bg-emerald-600 px-4 py-4 text-center text-2xl font-extrabold tracking-wide text-white"
      role="status"
    >
      ACTIVE — trading enabled
    </div>
  );
}
