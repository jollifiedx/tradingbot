/**
 * Email + password login via Supabase Auth. This is the only auth method
 * built so far -- mandatory TOTP 2FA (per research/tech-stack.md §2 and
 * CLAUDE.md's "treat it like a bank login" framing) is NOT implemented yet.
 * That is a known follow-up, not a silent gap: flagged here and in this
 * task's report. Single allowlisted owner is enforced server-side
 * (app_owner check in every API route) -- a successful Supabase login for
 * anyone else simply gets 403s from the API, never sees bot data.
 */
import { useState, type FormEvent } from "react";
import { supabase } from "../lib/supabase";

export function LoginScreen() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    const { error: signInError } = await supabase.auth.signInWithPassword({
      email,
      password,
    });
    setSubmitting(false);
    if (signInError) {
      setError(signInError.message);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-neutral-50 p-4 dark:bg-neutral-950">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900"
      >
        <h1 className="mb-1 text-xl font-bold text-neutral-900 dark:text-neutral-50">
          TradingBot
        </h1>
        <p className="mb-5 text-sm text-neutral-500">Owner sign-in</p>

        <label className="mb-3 flex flex-col gap-1 text-sm">
          Email
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
          />
        </label>

        <label className="mb-4 flex flex-col gap-1 text-sm">
          Password
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
          />
        </label>

        {error && (
          <p className="mb-4 text-sm text-red-600" role="alert">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-neutral-900 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60 dark:bg-neutral-100 dark:text-neutral-900"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>

        <p className="mt-4 text-xs text-neutral-400">
          Two-factor authentication is not enabled yet -- follow-up before
          this account should be trusted with real money.
        </p>
      </form>
    </div>
  );
}
