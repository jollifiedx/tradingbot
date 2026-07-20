/**
 * The caps editor: buy-power cap, max daily loss, max per-trade cap,
 * staleness threshold. react-hook-form + zod (CLAUDE.md), validation
 * mirrors the backend's DB CHECK constraints so bad input is caught
 * client-side (422 would catch it too, but a fast local error is better on
 * a phone). Money fields stay strings from form input all the way to the
 * PATCH body -- never parsed to a JS number for submission (only
 * `formatMoney`, a display-only helper, ever calls `Number()` on them, and
 * that's for the confirmation summary text, not the submitted value).
 *
 * Risk parameters get the same fat-finger-safety confirmation step as the
 * freeze toggle: submitting the form opens the shared confirmation dialog
 * summarizing exactly what will change, and only an explicit "Yes, save
 * changes" tap calls PATCH.
 */
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { capsFormSchema, type CapsFormValues } from "../lib/settingsSchema";
import { useSettingsQuery, useUpdateSettingsMutation } from "../hooks/useSettings";
import type { SettingsUpdate } from "../hooks/useSettings";
import { useConfirmStore } from "../store/confirmStore";
import { formatMoney } from "../lib/money";

const FIELD_LABELS: Record<keyof CapsFormValues, string> = {
  buy_power_cap: "Buy-power cap",
  max_daily_loss: "Max daily loss",
  max_per_trade_cap: "Max per-trade cap",
  staleness_threshold_seconds: "Staleness threshold (seconds)",
};

export function CapsEditor() {
  const { data } = useSettingsQuery();
  const mutation = useUpdateSettingsMutation();
  const openConfirm = useConfirmStore((s) => s.open);

  const form = useForm<CapsFormValues>({
    resolver: zodResolver(capsFormSchema),
    defaultValues: {
      buy_power_cap: "0.00",
      max_daily_loss: "0.00",
      max_per_trade_cap: "0.00",
      staleness_threshold_seconds: 60,
    },
  });

  // Populate the form from the live settings row, but never clobber an
  // in-progress edit: only reset while the form is clean.
  useEffect(() => {
    if (data && !form.formState.isDirty) {
      form.reset({
        buy_power_cap: data.buy_power_cap,
        max_daily_loss: data.max_daily_loss,
        max_per_trade_cap: data.max_per_trade_cap,
        staleness_threshold_seconds: data.staleness_threshold_seconds,
      });
    }
    // form is stable from useForm; only re-run when the server row changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (!data) {
    return null;
  }

  function onSubmit(values: CapsFormValues) {
    const changes: SettingsUpdate = {};
    const summaryLines: string[] = [];

    if (values.buy_power_cap !== data!.buy_power_cap) {
      changes.buy_power_cap = values.buy_power_cap;
      summaryLines.push(
        `${FIELD_LABELS.buy_power_cap}: ${formatMoney(data!.buy_power_cap)} -> ${formatMoney(values.buy_power_cap)}`,
      );
    }
    if (values.max_daily_loss !== data!.max_daily_loss) {
      changes.max_daily_loss = values.max_daily_loss;
      summaryLines.push(
        `${FIELD_LABELS.max_daily_loss}: ${formatMoney(data!.max_daily_loss)} -> ${formatMoney(values.max_daily_loss)}`,
      );
    }
    if (values.max_per_trade_cap !== data!.max_per_trade_cap) {
      changes.max_per_trade_cap = values.max_per_trade_cap;
      summaryLines.push(
        `${FIELD_LABELS.max_per_trade_cap}: ${formatMoney(data!.max_per_trade_cap)} -> ${formatMoney(values.max_per_trade_cap)}`,
      );
    }
    if (values.staleness_threshold_seconds !== data!.staleness_threshold_seconds) {
      changes.staleness_threshold_seconds = values.staleness_threshold_seconds;
      summaryLines.push(
        `${FIELD_LABELS.staleness_threshold_seconds}: ${data!.staleness_threshold_seconds}s -> ${values.staleness_threshold_seconds}s`,
      );
    }

    if (summaryLines.length === 0) {
      return; // nothing changed -- nothing to confirm or submit
    }

    openConfirm({
      title: "Save cap changes?",
      description: summaryLines.join("\n"),
      confirmLabel: "Yes, save changes",
      danger: true,
      onConfirm: () => {
        mutation.mutate(changes, {
          onSuccess: () => form.reset(values),
        });
      },
    });
  }

  return (
    <form
      onSubmit={form.handleSubmit(onSubmit)}
      className="flex flex-col gap-4 rounded-lg border border-neutral-200 p-4 dark:border-neutral-700"
    >
      <h2 className="text-base font-semibold text-neutral-900 dark:text-neutral-50">
        Risk caps
      </h2>

      <label className="flex flex-col gap-1 text-sm">
        {FIELD_LABELS.buy_power_cap}
        <input
          type="text"
          inputMode="decimal"
          {...form.register("buy_power_cap")}
          className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
        />
        {form.formState.errors.buy_power_cap && (
          <span className="text-xs text-red-600">
            {form.formState.errors.buy_power_cap.message}
          </span>
        )}
      </label>

      <label className="flex flex-col gap-1 text-sm">
        {FIELD_LABELS.max_daily_loss}
        <input
          type="text"
          inputMode="decimal"
          {...form.register("max_daily_loss")}
          className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
        />
        {form.formState.errors.max_daily_loss && (
          <span className="text-xs text-red-600">
            {form.formState.errors.max_daily_loss.message}
          </span>
        )}
      </label>

      <label className="flex flex-col gap-1 text-sm">
        {FIELD_LABELS.max_per_trade_cap}
        <input
          type="text"
          inputMode="decimal"
          {...form.register("max_per_trade_cap")}
          className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
        />
        {form.formState.errors.max_per_trade_cap && (
          <span className="text-xs text-red-600">
            {form.formState.errors.max_per_trade_cap.message}
          </span>
        )}
      </label>

      <label className="flex flex-col gap-1 text-sm">
        {FIELD_LABELS.staleness_threshold_seconds}
        <input
          type="text"
          inputMode="numeric"
          {...form.register("staleness_threshold_seconds", { valueAsNumber: true })}
          className="rounded-md border border-neutral-300 px-3 py-2 dark:border-neutral-600 dark:bg-neutral-800"
        />
        {form.formState.errors.staleness_threshold_seconds && (
          <span className="text-xs text-red-600">
            {form.formState.errors.staleness_threshold_seconds.message}
          </span>
        )}
      </label>

      <button
        type="submit"
        disabled={mutation.isPending || !form.formState.isDirty}
        className="rounded-md bg-neutral-900 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
      >
        {mutation.isPending ? "Saving…" : "Save changes"}
      </button>
      {mutation.isError && (
        <p className="text-sm text-red-600" role="alert">
          Could not save: {mutation.error.message}
        </p>
      )}
    </form>
  );
}
