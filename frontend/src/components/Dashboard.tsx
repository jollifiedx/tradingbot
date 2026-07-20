/**
 * Esther's control panel. Freeze status is visible at the top of every
 * render path (loading, not-authorized, and the normal dashboard all keep
 * the banner in view once settings are readable) -- see FreezeBanner's own
 * docstring for the fail-closed-in-what-we-show reasoning.
 */
import { useEffect } from "react";
import { useAuth } from "../auth/AuthProvider";
import { useSettingsQuery } from "../hooks/useSettings";
import { AuthError } from "../api/client";
import { FreezeBanner } from "./FreezeBanner";
import { FreezeToggle } from "./FreezeToggle";
import { CapsEditor } from "./CapsEditor";
import { DecisionLog } from "./DecisionLog";
import { AccountPanel } from "./AccountPanel";
import { NotAuthorizedScreen } from "./NotAuthorizedScreen";

export function Dashboard() {
  const { signOut } = useAuth();
  const settingsQuery = useSettingsQuery();

  const authError =
    settingsQuery.error instanceof AuthError ? settingsQuery.error : null;

  useEffect(() => {
    // Expired/invalid token: sign out so the app falls back to the login
    // screen rather than showing a permanently broken dashboard.
    if (authError?.status === 401) {
      void signOut();
    }
  }, [authError, signOut]);

  if (authError?.status === 403) {
    return <NotAuthorizedScreen onSignOut={() => void signOut()} />;
  }

  return (
    <div className="min-h-screen bg-neutral-50 dark:bg-neutral-950">
      <FreezeBanner />
      <main className="mx-auto flex max-w-2xl flex-col gap-6 p-4">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-bold text-neutral-900 dark:text-neutral-50">
            TradingBot
          </h1>
          <button
            type="button"
            onClick={() => void signOut()}
            className="text-sm text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200"
          >
            Sign out
          </button>
        </div>

        <FreezeToggle />
        <AccountPanel />
        <CapsEditor />
        <DecisionLog />
      </main>
    </div>
  );
}
