/**
 * TanStack Query hook for the latest account equity snapshot (GET /account).
 * 404 is an EXPECTED state (no snapshot recorded yet -- the worker hasn't
 * run) and resolves to `null` rather than an error, so the UI can render a
 * friendly "no snapshot yet" panel instead of an error state.
 */
import { useQuery } from "@tanstack/react-query";
import { api, assertNotAuthError } from "../api/client";
import type { components } from "../api/schema";

export type EquitySnapshot = components["schemas"]["EquitySnapshot"];

async function fetchAccount(): Promise<EquitySnapshot | null> {
  const { data, error, response } = await api.GET("/account");
  if (response.status === 404) {
    return null;
  }
  assertNotAuthError(response.status);
  if (error || !data) {
    throw new Error("could not read account snapshot");
  }
  return data;
}

export function useAccountQuery() {
  return useQuery({
    queryKey: ["account"],
    queryFn: fetchAccount,
    refetchInterval: 30_000,
  });
}
