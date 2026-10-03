/**
 * The Accounts page's worked-out parts (design `Accounts Beginner.dc.html`, layout A): which
 * group each account sits in, grouped loans, the row labels and the net worth line. Pure (types
 * only from api.ts) so `scripts/accounts.test.ts` runs it under `node --test`.
 */
import type { Account, Category } from '../../api';

export type GroupId = 'cash' | 'cards' | 'loans' | 'investments' | 'other';

export interface GroupMeta {
  id: GroupId;
  name: string;
  /** Over the group total: "You have", "You owe", "Worth". */
  totalLabel: string;
  /** Icon and avatar hue (design: cash 268, cards 25, loans 60, investments 155). */
  hue: number;
  /** One SVG path (24×24, stroked). */
  icon: string;
}

/** In page order; "Other" only shows when there are any. */
export const GROUPS: GroupMeta[] = [
  { id: 'cash', name: 'Cash and banking', totalLabel: 'You have', hue: 268, icon: 'M3.5 9.5 12 4l8.5 5.5M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3.5 20.5h17' },
  { id: 'cards', name: 'Credit cards', totalLabel: 'You owe', hue: 25, icon: 'M3.5 6.5h17v11h-17zM3.5 10h17M7 14.5h3' },
  { id: 'loans', name: 'Loans', totalLabel: 'You owe', hue: 60, icon: 'M4 11 12 4.5 20 11M6 9.5V20h12V9.5M10 20v-5h4v5' },
  { id: 'investments', name: 'Investments', totalLabel: 'Worth', hue: 155, icon: 'm3.5 16.5 5.5-5.5 4 4 7.5-7.5M15 7.5h5.5V13' },
  { id: 'other', name: 'Other', totalLabel: 'Worth', hue: 300, icon: 'M4.5 8 12 4l7.5 4v8L12 20l-7.5-4zM4.5 8 12 12l7.5-4M12 12v8' },
];

export const GROUP_OF: Record<Category, GroupId> = {
  bank: 'cash',
  credit: 'cards',
  loan: 'loans',
  hsa: 'investments',
  retirement: 'investments',
  investment: 'investments',
  other: 'other',
};

export const groupMeta = (c: Category): GroupMeta => GROUPS.find((g) => g.id === GROUP_OF[c])!;

export const isDebt = (a: Pick<Account, 'category'>) => a.category === 'loan' || a.category === 'credit';

/** Money the user has (not a debt) below zero: shown with its minus sign and the word "overdrawn" (amber, never red). */
export const isOverdrawn = (a: Pick<Account, 'category' | 'current_balance'>) => !isDebt(a) && a.current_balance < -0.004;

/**
 * The amount a row shows: debts as what's owed (its size, no minus sign, next to "owed"); everything
 * else with its sign, so an overdrawn account reads "−$50.00 overdrawn", never as money the user has.
 */
export const shownAmount = (a: Pick<Account, 'category' | 'current_balance'>) => (isDebt(a) ? Math.abs(a.current_balance) : a.current_balance);

/** The "Update balance" box's starting text: the amount owed for debts, else the signed balance. */
export const balanceDraft = (a: Pick<Account, 'category' | 'current_balance'>) => shownAmount(a).toFixed(2);

/** One line in a group card: an account, or several loans from one lender shown as one row. */
export type ListRow =
  | { type: 'account'; account: Account }
  | {
      type: 'loans';
      /** Stable key: lender + group ("item:3|Student loans"). */
      key: string;
      /** The loan group's name (the user's own, or the automatic one like "Student loans"). */
      title: string;
      /** "Nelnet"; the bank's name for connected loans. */
      lender: string;
      accounts: Account[];
      total: number;
      /** Every loan in it is one the user adds themselves (else they all update on their own). */
      manual: boolean;
      /** Loans whose balance is 7+ days old. */
      staleCount: number;
      /** The lender needs the user to sign in again. */
      signIn: boolean;
    };

export interface AccountGroup extends GroupMeta {
  /** Visible accounts in this group. */
  accounts: Account[];
  rows: ListRow[];
  /** Sum of the balances (debts: what's owed, positive). */
  total: number;
}

export interface AccountsView {
  groups: AccountGroup[];
  hidden: Account[];
  /** Visible accounts only ("{N} accounts" in the header). */
  visibleCount: number;
  /** "What you have minus what you owe" over the visible accounts. */
  netWorth: number;
}

const cents = (v: number) => Math.round(v * 100);
const sum = (list: Account[]) => list.reduce((s, a) => s + cents(a.current_balance), 0) / 100;

/**
 * Who a loan is with, for grouping: its bank connection for connected loans, else the lender
 * the user typed (case and spacing ignored). null = can't tell, so it's never grouped.
 */
export function lenderKey(a: Account): string | null {
  if (a.source === 'plaid') return a.item_id !== null ? `item:${a.item_id}` : null;
  const name = (a.institution_name ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
  return name ? `name:${name}` : null;
}

/**
 * Loans (in their given order): two or more from the same lender in the same loan group become
 * one row, placed where the first of them was; everything else stays its own row.
 */
export function groupLoans(loans: Account[]): ListRow[] {
  const buckets = new Map<string, Account[]>();
  for (const a of loans) {
    const lender = lenderKey(a);
    if (!lender) continue;
    const key = `${lender}|${a.loan_group ?? ''}`;
    const list = buckets.get(key);
    if (list) list.push(a);
    else buckets.set(key, [a]);
  }
  const rows: ListRow[] = [];
  const placed = new Set<string>();
  for (const a of loans) {
    const lender = lenderKey(a);
    const key = lender ? `${lender}|${a.loan_group ?? ''}` : null;
    const members = key ? buckets.get(key)! : [a];
    if (members.length < 2 || key === null) {
      rows.push({ type: 'account', account: a });
      continue;
    }
    if (placed.has(key)) continue;
    placed.add(key);
    const first = members[0]!;
    rows.push({
      type: 'loans',
      key,
      title: first.loan_group ?? 'Loans',
      lender: first.institution_name ?? 'Your lender',
      accounts: members,
      total: sum(members),
      manual: members.every((m) => m.source === 'manual'),
      staleCount: members.filter((m) => !!m.stale).length,
      signIn: members.some((m) => m.connection?.status === 'login_required'),
    });
  }
  return rows;
}

/** The whole list: groups in page order (empty ones left out), hidden accounts apart, and the totals. */
export function groupAccounts(accounts: Account[]): AccountsView {
  const visible = accounts.filter((a) => !a.hidden);
  const hidden = accounts.filter((a) => a.hidden);
  const groups: AccountGroup[] = [];
  for (const g of GROUPS) {
    const list = visible.filter((a) => GROUP_OF[a.category] === g.id);
    if (!list.length) continue;
    const rows: ListRow[] = g.id === 'loans' ? groupLoans(list) : list.map((account) => ({ type: 'account', account }));
    groups.push({ ...g, accounts: list, rows, total: sum(list) });
  }
  const owe = sum(visible.filter(isDebt));
  const have = sum(visible.filter((a) => !isDebt(a)));
  return { groups, hidden, visibleCount: visible.length, netWorth: (cents(have) - cents(owe)) / 100 };
}

// ---------------------------------------------------------------- row words

/** The small label under a row's balance. */
export function balanceWord(a: Account): string {
  if (isDebt(a)) return a.current_balance < -0.004 ? 'in your favor' : 'owed';
  if (isOverdrawn(a)) return 'overdrawn';
  if (GROUP_OF[a.category] === 'cash') {
    const avail = a.available_balance;
    return avail === null || Math.abs(avail - a.current_balance) < 0.005 ? 'available' : 'balance';
  }
  return 'worth';
}

export type ChipTone = 'red' | 'amber' | 'neutral';
export interface Chip {
  tone: ChipTone;
  text: string;
}

/** The one label a row can carry: sign in again (red), update balance (amber), or "You add this one". */
export function accountChip(a: Account): Chip | null {
  if (a.connection?.status === 'login_required') return { tone: 'red', text: 'Sign in again' };
  if (a.source === 'manual' && a.stale) return { tone: 'amber', text: 'Update balance' };
  if (a.source === 'manual') return { tone: 'neutral', text: 'You add this one' };
  return null;
}

/** A grouped-loans row's label. */
export function loansChip(row: Extract<ListRow, { type: 'loans' }>): Chip | null {
  if (row.signIn) return { tone: 'red', text: 'Sign in again' };
  if (row.staleCount > 0) return { tone: 'amber', text: row.staleCount === 1 ? '1 needs a new balance' : `${row.staleCount} need new balances` };
  return null;
}

const monthDay = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });

/** "you updated it 12 days ago" (manual) or "updated 2 hours ago" / "last updated Sep 22" (connected). */
export function updatedWords(a: Account, now: Date = new Date()): string {
  if (a.source === 'manual') {
    const d = a.balance_age_days;
    if (d === null || d === undefined) return 'no balance saved yet';
    if (d <= 0) return 'you updated it today';
    if (d === 1) return 'you updated it yesterday';
    return `you updated it ${d} days ago`;
  }
  const c = a.connection;
  const at = c?.last_synced_at ?? null;
  if (!at) return 'not updated yet';
  const t = new Date(at);
  if (Number.isNaN(t.getTime())) return 'not updated yet';
  if (c && c.status !== 'ok') return `last updated ${monthDay.format(t)}`;
  const mins = Math.floor((now.getTime() - t.getTime()) / 60_000);
  if (mins < 1) return 'updated just now';
  if (mins < 60) return `updated ${mins} minute${mins === 1 ? '' : 's'} ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `updated ${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.max(1, Math.round((startOfDay(now) - startOfDay(t)) / 86_400_000));
  return days === 1 ? 'updated yesterday' : `updated ${days} days ago`;
}

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** "Nelnet ··4417 · you updated it 2 days ago" (parts the user doesn't have are left out). */
export function rowSubline(a: Account, now: Date = new Date()): string {
  const bank = a.institution_name?.trim();
  const first = [bank, a.mask ? `··${a.mask}` : null].filter(Boolean).join(' ');
  return [first, updatedWords(a, now)].filter(Boolean).join(' · ');
}

// ---------------------------------------------------------------- detail words

const SUBTYPE_LABEL: Record<string, string> = {
  checking: 'Checking',
  savings: 'Savings',
  'money market': 'Money market',
  cd: 'CD',
  'cash management': 'Cash account',
  prepaid: 'Prepaid card',
  paypal: 'PayPal',
  'credit card': 'Credit card',
  student: 'Student loan',
  auto: 'Car loan',
  mortgage: 'Mortgage',
  'home equity': 'Home equity loan',
  'line of credit': 'Line of credit',
  personal: 'Personal loan',
  loan: 'Loan',
  '401k': '401(k)',
  '403b': '403(b)',
  '457b': '457(b)',
  ira: 'IRA',
  roth: 'Roth IRA',
  'roth 401k': 'Roth 401(k)',
  'sep ira': 'SEP IRA',
  'simple ira': 'SIMPLE IRA',
  pension: 'Pension',
  hsa: 'Health savings',
  '529': '529 college savings',
  brokerage: 'Investment account',
};

const CATEGORY_TYPE: Record<Category, string> = {
  bank: 'Bank account',
  credit: 'Credit card',
  loan: 'Loan',
  hsa: 'Health savings',
  retirement: 'Retirement',
  investment: 'Investment account',
  other: 'Something you own',
};

/** "Checking", "Student loan", "401(k)"…: the subtype when FinTrack knows a plain name for it. */
export function typeLabel(a: Pick<Account, 'plaid_subtype' | 'category'>): string {
  const sub = a.plaid_subtype?.trim().toLowerCase();
  return (sub && SUBTYPE_LABEL[sub]) || CATEGORY_TYPE[a.category];
}

/** The balance's heading on the detail page ("Overdrawn by" + the size: "Overdrawn by $50.00"). */
export function balanceHeading(a: Account): string {
  if (isDebt(a)) return a.current_balance < -0.004 ? 'In your favor' : 'You owe';
  if (isOverdrawn(a)) return 'Overdrawn by';
  if (GROUP_OF[a.category] === 'cash') return balanceWord(a) === 'available' ? 'Available now' : 'Balance now';
  return 'Worth now';
}

/** A typed dollar amount ("$1,250.50", "−20"): null when empty, NaN when it isn't a number with up to 2 decimals. */
export function parseAmount(raw: string): number | null {
  const cleaned = raw.replace(/[\s$,]/g, '').replace(/−/g, '-');
  if (cleaned === '' || cleaned === '-' || cleaned === '.') return null;
  if (!/^-?\d*(\.\d{0,2})?$/.test(cleaned)) return NaN;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : NaN;
}
