/**
 * Renders the single global confirmation modal driven by `useConfirmStore`.
 * Mount this once, near the root. This is the fat-finger-safety gate for
 * every mutating action in the app (freeze/unfreeze, cap changes): nothing
 * should call an API mutation directly from a click handler without routing
 * through this dialog first.
 */
import { useEffect, useRef } from "react";
import { useConfirmStore } from "../store/confirmStore";

export function ConfirmDialogHost() {
  const request = useConfirmStore((s) => s.request);
  const close = useConfirmStore((s) => s.close);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (request) {
      confirmButtonRef.current?.focus();
    }
  }, [request]);

  if (!request) {
    return null;
  }

  const handleConfirm = () => {
    request.onConfirm();
    close();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
    >
      <div className="w-full max-w-sm rounded-lg bg-white p-5 shadow-xl dark:bg-neutral-900">
        <h2
          id="confirm-dialog-title"
          className="text-lg font-semibold text-neutral-900 dark:text-neutral-50"
        >
          {request.title}
        </h2>
        <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-300">
          {request.description}
        </p>
        <div className="mt-5 flex gap-3">
          <button
            type="button"
            onClick={close}
            className="flex-1 rounded-md border border-neutral-300 px-4 py-2.5 text-sm font-medium text-neutral-700 hover:bg-neutral-100 dark:border-neutral-700 dark:text-neutral-200 dark:hover:bg-neutral-800"
          >
            Cancel
          </button>
          <button
            type="button"
            ref={confirmButtonRef}
            onClick={handleConfirm}
            className={
              "flex-1 rounded-md px-4 py-2.5 text-sm font-semibold text-white " +
              (request.danger
                ? "bg-red-600 hover:bg-red-700"
                : "bg-emerald-600 hover:bg-emerald-700")
            }
          >
            {request.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
