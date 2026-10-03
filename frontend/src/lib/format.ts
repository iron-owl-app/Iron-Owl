import type { Category } from '../api';

const currencyCache = new Map<string, Intl.NumberFormat>();

function currencyFormatter(currency: string, opts: { cents: boolean; compact: boolean }): Intl.NumberFormat {
  const key = `${currency}|${opts.cents}|${opts.compact}`;
  let f = currencyCache.get(key);
  if (!f) {
    try {
      f = new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency,
        notation: opts.compact ? 'compact' : 'standard',
        minimumFractionDigits: opts.compact ? 0 : opts.cents ? 2 : 0,
        maximumFractionDigits: opts.compact ? 1 : opts.cents ? 2 : 0,
      });
    } catch {
      // Unknown currency code from the backend: fall back to USD formatting.
      f = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' });
    }
    currencyCache.set(key, f);
  }
  return f;
}

export interface MoneyOptions {
  currency?: string;
  cents?: boolean;
  compact?: boolean;
  /** Always show a leading sign (+/−). */
  signed?: boolean;
}

/** Formats dollars. Uses a true minus sign (U+2212) for negatives. */
export function formatMoney(value: number, opts: MoneyOptions = {}): string {
  const { currency = 'USD', cents = true, compact = false, signed = false } = opts;
  const abs = Math.abs(value);
  const body = currencyFormatter(currency, { cents, compact }).format(abs);
  const isZero = abs < 0.005;
  if (value < 0 && !isZero) return `−${body}`;
  if (signed && !isZero) return `+${body}`;
  return body;
}

const pctFmt = new Intl.NumberFormat(undefined, { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
export function formatPercent(ratio: number, signed = false): string {
  const s = pctFmt.format(Math.abs(ratio));
  if (ratio < 0 && Math.abs(ratio) >= 0.0005) return `−${s}`;
  if (signed && ratio > 0) return `+${s}`;
  return s;
}

const numFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 });
export function formatQuantity(n: number): string {
  return numFmt.format(n);
}

const rateFmt = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
export function formatRate(pct: number): string {
  return `${rateFmt.format(pct)}%`;
}

/** Parse an ISO `YYYY-MM-DD` as a *local* calendar date (no UTC shift). */
export function parseISODate(iso: string): Date {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1);
}

export function toISODate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

const dateFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const dateShortFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const monthYearFmt = new Intl.DateTimeFormat(undefined, { month: 'short', year: 'numeric' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

export function formatDate(iso: string): string {
  return dateFmt.format(parseISODate(iso));
}

/** "Mar 4" this year, "Mar 4, 2024" otherwise. */
export function formatDateSmart(iso: string): string {
  const d = parseISODate(iso);
  return d.getFullYear() === new Date().getFullYear() ? dateShortFmt.format(d) : dateFmt.format(d);
}

export function formatMonthYear(d: Date): string {
  return monthYearFmt.format(d);
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dateTimeFmt.format(d);
}

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

/** "just now", "5 minutes ago", "yesterday", … */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const diffSec = Math.round((t - Date.now()) / 1000);
  const abs = Math.abs(diffSec);
  if (abs < 45) return 'just now';
  if (abs < 3600) return rtf.format(Math.round(diffSec / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), 'hour');
  if (abs < 86400 * 30) return rtf.format(Math.round(diffSec / 86400), 'day');
  return formatDateTime(iso);
}

/**
 * The shared "Updated …" words (sidebar, Accounts, Home and Reports headers), as a whole phrase:
 * "Updated just now", "Updated 5 minutes ago", "Updated 1 hour ago", "Updated yesterday",
 * "Updated 3 days ago"; null (never updated) → "Not updated yet". Minutes and hours while it's
 * under a day old, then calendar days. A time in the future (clock drift) counts as just now.
 */
export function formatUpdated(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return 'Not updated yet';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return 'Not updated yet';
  const mins = Math.floor((now.getTime() - t.getTime()) / 60_000);
  if (mins < 1) return 'Updated just now';
  if (mins < 60) return `Updated ${mins} minute${mins === 1 ? '' : 's'} ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `Updated ${hours} hour${hours === 1 ? '' : 's'} ago`;
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.max(1, Math.round((day(now) - day(t)) / 86_400_000));
  return days === 1 ? 'Updated yesterday' : `Updated ${days} days ago`;
}

/** Whole days from today (local) to an ISO date. Negative = in the past. */
export function daysUntil(iso: string): number {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((parseISODate(iso).getTime() - today.getTime()) / 86400000);
}

export const CATEGORY_LABEL: Record<Category, string> = {
  bank: 'Cash & banking',
  hsa: 'HSA',
  retirement: 'Retirement',
  investment: 'Investments',
  loan: 'Loans',
  credit: 'Credit cards',
  other: 'Other assets',
};

export const CATEGORY_SINGULAR: Record<Category, string> = {
  bank: 'Bank account',
  hsa: 'HSA',
  retirement: 'Retirement (401k, IRA…)',
  investment: 'Brokerage / investment',
  loan: 'Loan',
  credit: 'Credit card',
  other: 'Other asset',
};

/** "FOOD_AND_DRINK" → "Food and drink" */
export function humanizeCategory(c: string | null): string {
  if (!c) return 'Uncategorized';
  const s = c.replace(/_/g, ' ').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Stable hue (0–359) from a string, for initial avatars. */
export function hueFor(name: string): number {
  let h = 2166136261;
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return Math.abs(h) % 360;
}

export function initialsFor(name: string): string {
  const words = name
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .split(/\s+/)
    .filter((w) => w && !['the', 'of', 'and', 'bank', 'na', 'n.a.'].includes(w.toLowerCase()));
  const src = words.length ? words : name.split(/\s+/).filter(Boolean);
  const a = src[0]?.charAt(0) ?? '?';
  const b = src[1]?.charAt(0) ?? '';
  return (a + b).toUpperCase();
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
