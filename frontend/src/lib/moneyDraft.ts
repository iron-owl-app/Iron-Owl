/**
 * The decision behind `useMoneyDraft` (a whole-dollar amount typed into a box, saved on blur or
 * Enter). Free of runtime imports so `npm run test:unit` can load it (scripts/recurringPanels.test.ts).
 */
export type MoneyDraftDecision =
  /** Escape: put the saved value back, save nothing. */
  | { kind: 'reset' }
  /** Not a usable amount: show the error, put the saved value back. */
  | { kind: 'error'; message: string }
  /** The same whole-dollar amount: nothing to save. */
  | { kind: 'same'; value: number }
  | { kind: 'save'; value: number };

export const MONEY_DRAFT_ERROR = 'Use an amount more than $0, like 2,000.';

export function moneyDraftDecision(draft: string, saved: number, cancelled: boolean): MoneyDraftDecision {
  if (cancelled) return { kind: 'reset' };
  const n = Number(draft.replace(/[$,\s]/g, ''));
  if (!draft.trim() || !Number.isFinite(n) || n <= 0) return { kind: 'error', message: MONEY_DRAFT_ERROR };
  const v = Math.round(n);
  return v === saved ? { kind: 'same', value: v } : { kind: 'save', value: v };
}
