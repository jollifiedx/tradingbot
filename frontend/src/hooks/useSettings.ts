/**
 * TanStack Query hooks for the `settings` control surface (GET + PATCH
 * /settings). Server state lives here, not in Zustand. Money fields stay
 * strings end to end -- see `src/lib/money.ts`.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, assertNotAuthError } from "../api/client";
import type { components } from "../api/schema";

export type BotSettings = components["schemas"]["BotSettings"];
export type SettingsUpdate = components["schemas"]["SettingsUpdate"];

export const SETTINGS_QUERY_KEY = ["settings"] as const;

async function fetchSettings(): Promise<BotSettings> {
  const { data, error, response } = await api.GET("/settings");
  assertNotAuthError(response.status);
  if (error || !data) {
    throw new Error("could not read settings");
  }
  return data;
}

export function useSettingsQuery() {
  return useQuery({
    queryKey: SETTINGS_QUERY_KEY,
    queryFn: fetchSettings,
    // Freeze state must be visible and current -- poll so a change made
    // elsewhere (or by the worker's own halt) shows up without a manual
    // refresh.
    refetchInterval: 15_000,
  });
}

async function patchSettings(body: SettingsUpdate): Promise<BotSettings> {
  const { data, error, response } = await api.PATCH("/settings", {
    body,
  });
  assertNotAuthError(response.status);
  if (error || !data) {
    const detail = error?.detail
      ? JSON.stringify(error.detail)
      : "could not update settings";
    throw new Error(detail);
  }
  return data;
}

export function useUpdateSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: patchSettings,
    onSuccess: (data) => {
      queryClient.setQueryData(SETTINGS_QUERY_KEY, data);
    },
  });
}
