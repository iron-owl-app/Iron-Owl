/**
 * The "Add an account yourself" form's rules (Release 3.10): the kinds, and checking what the user typed
 * into the request body. Pure, so `scripts/accounts.test.ts` runs it under `node --test`.
 */
import type { ManualKind, NewAccountByKind } from '../api';

/**
 * "What kind?" tiles (design `Accounts Beginner.dc.html`); the kind sets the group on Accounts.
 * `negative`: the user can type a balance below zero (an overdrawn account). Debts are typed as the
 * amount owed, so never negative.
 */
export const MANUAL_KINDS: { kind: ManualKind; label: string; debt: boolean; negative: boolean; example: string }[] = [
  { kind: 'checking', label: 'Checking', debt: false, negative: true, example: 'Everyday checking' },
  { kind: 'savings', label: 'Savings', debt: false, negative: true, example: 'Rainy day savings' },
  { kind: 'credit_card', label: 'Credit card', debt: true, negative: false, example: 'Store card' },
  { kind: 'student', label: 'Student loan', debt: true, negative: false, example: 'Federal student loan' },
  { kind: 'auto', label: 'Car loan', debt: true, negative: false, example: 'Car loan' },
  { kind: 'other_loan', label: 'Other loan', debt: true, negative: false, example: 'Personal loan' },
  { kind: 'investment', label: 'Investment', debt: false, negative: false, example: 'Retirement account' },
  { kind: 'other', label: 'Something else you own (like a house or car)', debt: false, negative: true, example: 'House' },
];

/** Days before FinTrack asks for a new balance (the backend's `stale_days`): a house or car changes slowly. */
export const remindDays = (category: string | null | undefined): number => (category === 'other' ? 30 : 7);

export interface ManualDraft {
  kind: ManualKind | null;
  name: string;
  bank: string;
  last4: string;
  balance: string;
  rate: string;
  payment: string;
}

export const EMPTY_MANUAL: ManualDraft = { kind: null, name: '', bank: '', last4: '', balance: '', rate: '', payment: '' };

export type ManualErrors = Partial<Record<keyof ManualDraft, string>>;

/** A dollar amount: null when empty, NaN when it isn't a number with up to 2 decimals (a leading minus only when `negative`). */
function amount(raw: string, negative = false): number | null {
  const cleaned = raw.replace(/[\s$,]/g, '').replace(/−/g, '-');
  if (cleaned === '' || cleaned === '.' || cleaned === '-' || cleaned === '-.') return null;
  if (!(negative ? /^-?\d*(\.\d{0,2})?$/ : /^\d*(\.\d{0,2})?$/).test(cleaned)) return NaN;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : NaN;
}

/** Checks the draft: the request body, or the first problem for each field. */
export function checkManual(d: ManualDraft): { body: NewAccountByKind | null; errors: ManualErrors } {
  const e: ManualErrors = {};
  const kind = MANUAL_KINDS.find((k) => k.kind === d.kind);
  if (!kind) e.kind = 'Pick what kind of account it is.';
  const name = d.name.trim();
  if (!name) e.name = 'Type a name for it.';
  const bal = amount(d.balance, !!kind?.negative);
  if (bal === null) e.balance = kind?.debt ? 'Type how much you owe now.' : 'Type the balance now.';
  else if (Number.isNaN(bal)) e.balance = kind?.negative ? 'Type an amount like 1250.00, or -50.00 if it’s overdrawn' : 'Type an amount like 1250.00';
  const last4 = d.last4.trim();
  if (last4 && !/^\d{4}$/.test(last4)) e.last4 = 'Type all 4 digits, or leave it empty.';
  let rate: number | null = null;
  let payment: number | null = null;
  if (kind?.debt) {
    if (d.rate.trim()) {
      rate = Number(d.rate.replace(/[%\s]/g, ''));
      if (!Number.isFinite(rate) || rate < 0 || rate > 100) e.rate = 'Type a number from 0 to 100, like 6.5';
    }
    payment = amount(d.payment);
    if (payment !== null && Number.isNaN(payment)) e.payment = 'Type an amount like 95.00';
  }
  if (Object.keys(e).length || !kind || bal === null) return { body: null, errors: e };
  const body: NewAccountByKind = { name, kind: kind.kind, current_balance: bal };
  if (d.bank.trim()) body.institution_name = d.bank.trim();
  if (last4) body.mask = last4;
  if (kind.debt && rate !== null) body.interest_rate = rate;
  if (kind.debt && payment !== null) body.minimum_payment = payment;
  return { body, errors: {} };
}

/** Enough typed to try adding it (the button stays off until then). */
export const manualReady = (d: ManualDraft) => d.kind !== null && d.name.trim() !== '' && d.balance.trim() !== '';
