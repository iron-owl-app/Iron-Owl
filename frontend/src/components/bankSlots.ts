/**
 * Bank connections used (Release 3.10): the Plaid Trial plan allows 10 connections, ever. The
 * count comes from GET /api/plaid/status; every place that starts Plaid Link for a new bank checks
 * it first and says so plainly when all are used. Pure, so `scripts/accounts.test.ts` runs it.
 */
import type { PlaidStatus } from '../api';

export interface BankSlots {
  /** Connections used so far (lifetime). */
  used: number;
  /** The plan's limit (10). */
  limit: number;
  /** Connections there are now: the lowest "used" count the owner can set. */
  now: number;
}

/**
 * The counts from GET /api/plaid/status, or null when the server didn't send them or counting is
 * off (Iron Owl 2.0.0: `items_cap` false, `items_limit` 0): then nothing is shown or blocked.
 */
export function slotsOf(s: PlaidStatus | null | undefined): BankSlots | null {
  if (!s || s.items_cap === false) return null;
  if (typeof s.items_linked !== 'number' || typeof s.items_limit !== 'number' || s.items_limit <= 0) return null;
  const now = typeof s.items_now === 'number' && s.items_now >= 0 ? s.items_now : 0;
  return { used: s.items_linked, limit: s.items_limit, now };
}

/** Every connection is used: a new bank can't be connected (signing in again to a bank still works). */
export const slotsFull = (s: BankSlots | null | undefined): boolean => !!s && s.used >= s.limit;

/** The one plain message wherever a new connection would start. */
export const allUsedText = (limit: number) => `You’ve used all ${limit} bank connections. You can still add accounts yourself.`;

/** Settings › Banks row. */
export const usedRowText = (s: BankSlots) => `Bank connections used: ${s.used} of ${s.limit}`;

/**
 * The "Change…" window's box: a whole number from the connections there are now up to the limit.
 * Returns the number, or the plain words to show under the box.
 */
export function checkUsedCount(raw: string, s: BankSlots): { value: number; error: null } | { value: null; error: string } {
  const text = raw.trim();
  const range = `Type a whole number from ${s.now} to ${s.limit}.`;
  if (!/^\d{1,2}$/.test(text)) return { value: null, error: range };
  const n = Number(text);
  if (n < s.now) return { value: null, error: `${range} You have ${s.now} connected now, so it can’t be less.` };
  if (n > s.limit) return { value: null, error: range };
  return { value: n, error: null };
}
