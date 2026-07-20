/**
 * UI-only state (Zustand, per CLAUDE.md -- server state lives in TanStack
 * Query, this store never holds API data). A single global confirmation
 * dialog that any mutating action (freeze/unfreeze, caps edits) can request
 * before it fires. Centralizing it means every fat-finger-safety
 * confirmation in the app looks and behaves identically, and no component
 * needs to build its own modal.
 */
import { create } from "zustand";

export interface ConfirmRequest {
  title: string;
  description: string;
  confirmLabel: string;
  /** Styles the confirm button as destructive (e.g. freezing the bot). */
  danger?: boolean;
  onConfirm: () => void;
}

interface ConfirmState {
  request: ConfirmRequest | null;
  open: (request: ConfirmRequest) => void;
  close: () => void;
}

export const useConfirmStore = create<ConfirmState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
