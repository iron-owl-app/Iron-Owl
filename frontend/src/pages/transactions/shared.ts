import { useEffect, useState } from 'react';
import type { CategorySource, Rule, Transaction, TxnCategory } from '../../api';
import { formatMoney, parseISODate } from '../../lib/format';

/** Longest "Always for …" rule text the app will create. */
export const MAX_RULE_TEXT = 200;

export const txnTitle = (t: Pick<Transaction, 'merchant_name' | 'name'>) => t.merchant_name || t.name;

/**
 * Merchant key like the backend's `recurring.merchant_key` merchant branch: the merchant name
 * (the bank's name when blank), lowercased, whitespace collapsed, at most 120 characters.
 */
export const merchantKey = (t: Pick<Transaction, 'merchant_name' | 'name'>) =>
  (t.merchant_name?.trim() ? t.merchant_name : (t.name ?? '')).toLowerCase().split(/\s+/).filter(Boolean).join(' ').slice(0, 120);

/** Rule matcher for "Always for {merchant}": the merchant when there is one, else the bank's name. */
export function ruleMatchFor(t: Pick<Transaction, 'merchant_name' | 'name'>): { field: 'merchant' | 'name'; text: string } {
  return t.merchant_name ? { field: 'merchant', text: t.merchant_name.trim() } : { field: 'name', text: t.name.trim() };
}

export const SOURCE_TEXT: Record<CategorySource, string> = {
  user: 'Set by you',
  user_bulk: 'Set by you (several at once)',
  rule: 'Your rule',
  auto: 'Sorted automatically',
  plaid: 'From your bank',
};

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric' });
/** "Friday, Sep 26, 2026" */
export const formatDayLong = (iso: string) => dayFmt.format(parseISODate(iso));

/** "Rule: merchant is “Starbucks” → Coffee" */
export function ruleText(r: Rule, categories: ReadonlyMap<string, TxnCategory>): string {
  const field = r.field === 'merchant' ? 'merchant' : r.field === 'name' ? 'name' : 'merchant or name';
  const amount = r.amount_op && r.amount !== null ? ` and ${r.amount_op === 'gt' ? 'over' : 'under'} ${formatMoney(r.amount)}` : '';
  const to = r.action === 'transfer' ? 'Transfer' : (categories.get(r.category ?? '')?.name ?? 'a deleted category');
  return `Rule: ${field} ${r.op === 'is' ? 'is' : 'contains'} “${r.text}”${amount} → ${to}`;
}

/** Matches a CSS media query, live. */
export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const update = () => setOn(mq.matches);
    update();
    mq.addEventListener('change', update);
    return () => mq.removeEventListener('change', update);
  }, [query]);
  return on;
}
