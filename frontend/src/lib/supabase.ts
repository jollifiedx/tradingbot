/**
 * Supabase Auth client. This is the ONLY thing the UI uses Supabase for:
 * signing Esther in and holding her session so its access_token can be sent
 * as a Bearer token to the TradingBot API. The UI never reads/writes
 * application tables directly through this client (Invariant 2 -- all
 * backend data flows through the generated API client in `src/api/`).
 *
 * Only the anon key lives here (safe for the browser). The service_role key
 * and every broker/LLM credential must never appear in frontend code.
 */
import { createClient } from "@supabase/supabase-js";

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error(
    "Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY -- check frontend/.env",
  );
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey);
