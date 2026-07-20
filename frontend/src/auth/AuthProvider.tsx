/**
 * Auth session provider.
 *
 * Holds the Supabase Auth session (email+password login only for now --
 * TOTP/2FA enrollment is not built yet; see CLAUDE.md's mandatory-2FA intent
 * and README follow-up note). This is neither TanStack Query "server state"
 * (it's push-driven via Supabase's own listener, not fetched/cached by us)
 * nor Zustand "UI state" (it's identity, not a UI toggle) -- a small React
 * context is the right tool and keeps both conventions honest.
 *
 * The session's access_token is attached to every API request by
 * `src/api/client.ts`; this provider never talks to the TradingBot API
 * itself and never talks to Webull (Invariant 2).
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import { supabase } from "../lib/supabase";

interface AuthContextValue {
  session: Session | null;
  /** True until the initial session lookup resolves. */
  loading: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });

    const { data: subscription } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession);
    });

    return () => {
      subscription.subscription.unsubscribe();
    };
  }, []);

  async function signOut() {
    await supabase.auth.signOut();
  }

  return (
    <AuthContext.Provider value={{ session, loading, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return ctx;
}
