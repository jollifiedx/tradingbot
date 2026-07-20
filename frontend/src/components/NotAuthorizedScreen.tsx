/**
 * Shown when a Supabase login succeeds but the API's single-owner allowlist
 * (`app_owner`) rejects the user (403). This app is single-tenant forever
 * (CLAUDE.md) -- there is no "request access" flow, just a clear message
 * and a way to sign out and try a different account.
 */
export function NotAuthorizedScreen({ onSignOut }: { onSignOut: () => void }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-neutral-50 p-4 text-center dark:bg-neutral-950">
      <h1 className="text-xl font-bold text-neutral-900 dark:text-neutral-50">
        Not authorized
      </h1>
      <p className="max-w-sm text-sm text-neutral-500">
        This account is signed in but is not the TradingBot owner. This
        dashboard has exactly one authorized user.
      </p>
      <button
        type="button"
        onClick={onSignOut}
        className="rounded-md border border-neutral-300 px-4 py-2 text-sm font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
      >
        Sign out
      </button>
    </div>
  );
}
