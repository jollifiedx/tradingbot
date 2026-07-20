/** TanStack Query hook for the append-only decision log (GET /decisions). */
import { useQuery } from "@tanstack/react-query";
import { api, assertNotAuthError } from "../api/client";
import type { components } from "../api/schema";

export type Decision = components["schemas"]["Decision"];

async function fetchDecisions(): Promise<Decision[]> {
  const { data, error, response } = await api.GET("/decisions", {
    params: { query: { limit: 50, offset: 0 } },
  });
  assertNotAuthError(response.status);
  if (error || !data) {
    throw new Error("could not read decisions");
  }
  return data;
}

export function useDecisionsQuery() {
  return useQuery({
    queryKey: ["decisions"],
    queryFn: fetchDecisions,
    refetchInterval: 30_000,
  });
}
