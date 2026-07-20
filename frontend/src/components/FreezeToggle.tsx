/**
 * The kill switch. Calls PATCH /settings {frozen} -- through the generated
 * API client only, never a hand-written fetch. This mutates `settings` in
 * Supabase; the worker reads `settings` before every order and obeys
 * (Invariant 2). Every click routes through the global confirmation dialog
 * first (fat-finger safety) -- there is no path from a single tap to a
 * freeze-state change.
 */
import { useConfirmStore } from "../store/confirmStore";
import { useSettingsQuery, useUpdateSettingsMutation } from "../hooks/useSettings";

export function FreezeToggle() {
  const { data } = useSettingsQuery();
  const mutation = useUpdateSettingsMutation();
  const openConfirm = useConfirmStore((s) => s.open);

  if (!data) {
    return null;
  }

  const isFrozen = data.frozen;
  const actionLabel = isFrozen ? "Unfreeze bot" : "Freeze bot";

  function handleClick() {
    openConfirm({
      title: isFrozen ? "Unfreeze the bot?" : "Freeze the bot?",
      description: isFrozen
        ? "The bot will be allowed to place orders again on its next check. Only do this if you're sure it's safe to resume trading."
        : "The bot will stop placing new orders immediately on its next settings check. Use this any time something looks wrong.",
      confirmLabel: isFrozen ? "Yes, unfreeze" : "Yes, freeze now",
      danger: !isFrozen,
      onConfirm: () => {
        mutation.mutate({ frozen: !isFrozen });
      },
    });
  }

  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        onClick={handleClick}
        disabled={mutation.isPending}
        className={
          "w-full rounded-md px-4 py-3 text-base font-bold text-white transition disabled:opacity-60 " +
          (isFrozen
            ? "bg-emerald-600 hover:bg-emerald-700"
            : "bg-red-600 hover:bg-red-700")
        }
      >
        {mutation.isPending ? "Updating…" : actionLabel}
      </button>
      {mutation.isError && (
        <p className="text-sm text-red-600" role="alert">
          Could not update freeze state: {mutation.error.message}
        </p>
      )}
    </div>
  );
}
