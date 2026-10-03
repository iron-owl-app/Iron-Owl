/**
 * Reports › Paying off debt: the sentences and numbers worked out on the client (design
 * "Debt Plan", layout B; the plan's "Honesty rules"). The payoff itself is the server's
 * (POST /api/debt/plan); only "Try a different monthly payment" (one debt on its own) is
 * worked out here, with the same cents rules. Kept free of runtime imports so
 * `npm run test:unit` can load it directly (scripts/debt.test.ts).
 */
import type { DebtKind, DebtPlanDebt, DebtPlanV2, DebtProgress } from '../../../api';

// ---------------------------------------------------------------- formatting

const fmtCache = new Map<boolean, Intl.NumberFormat>();
/** "$1,234" or, with cents, "$1,234.56". Never a sign: the words say which way. */
export function money(v: number, cents = false): string {
  let f = fmtCache.get(cents);
  if (!f) {
    f = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
    fmtCache.set(cents, f);
  }
  return f.format(Math.abs(v));
}

const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
/** 'YYYY-MM' or 'YYYY-MM-DD' → 'Oct 2029'. */
export const monYear = (d: string) => `${SHORT[Number(d.slice(5, 7)) - 1] ?? ''} ${d.slice(0, 4)}`;
/** 'YYYY-MM' → 'October 2025'. */
export const monthYearLong = (d: string) => `${LONG[Number(d.slice(5, 7)) - 1] ?? ''} ${d.slice(0, 4)}`;

/** 'YYYY-MM' plus n months. */
export function addMonths(m: string, n: number): string {
  const t = Number(m.slice(0, 4)) * 12 + Number(m.slice(5, 7)) - 1 + n;
  return `${Math.floor(t / 12)}-${String((t % 12) + 1).padStart(2, '0')}`;
}

/** 83 → "6 years 11 months"; 12 → "1 year"; 1 → "1 month". */
export function dur(months: number): string {
  const y = Math.floor(months / 12);
  const r = months % 12;
  const p: string[] = [];
  if (y) p.push(y === 1 ? '1 year' : `${y} years`);
  if (r) p.push(r === 1 ? '1 month' : `${r} months`);
  return p.join(' ') || 'no time';
}

/** 24.99 → "24.99"; 6.10 → "6.1". */
export function aprText(apr: number): string {
  return String(Math.round(apr * 100) / 100);
}

/** What the user typed in a $ field: digits, commas and one dot; null when it isn't a positive amount. */
export function parseMoney(s: string): number | null {
  const t = s.replace(/[$,\s]/g, '');
  if (!/^\d*\.?\d*$/.test(t) || t === '' || t === '.') return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n <= 0 || n > 1_000_000_000) return null;
  return Math.round(n * 100) / 100;
}

// ---------------------------------------------------------------- what to include

/** The UI pref `fintrack.ui.debt.excluded`: unchecked debts, and every debt seen before (so a new mortgage starts unchecked). */
export interface IncludePref {
  excluded: number[];
  seen: number[];
}

const isIdList = (v: unknown): v is number[] => Array.isArray(v) && v.every((x) => typeof x === 'number' && Number.isInteger(x));
export const isIncludePref = (v: unknown): v is IncludePref =>
  !!v && typeof v === 'object' && isIdList((v as IncludePref).excluded) && isIdList((v as IncludePref).seen);

/**
 * Which debts are unchecked: what the user unchecked before, plus any mortgage FinTrack hasn't
 * shown the user yet (mortgages start unchecked). At least one debt always stays checked.
 */
export function resolveExcluded(pref: IncludePref | null, debts: { account_id: number; kind: DebtKind }[]): IncludePref {
  const ids = debts.map((d) => d.account_id);
  const seen = new Set(pref?.seen ?? []);
  const excluded = (pref?.excluded ?? []).filter((id) => ids.includes(id));
  for (const d of debts) if (!seen.has(d.account_id) && d.kind === 'mortgage' && !excluded.includes(d.account_id)) excluded.push(d.account_id);
  if (ids.length && excluded.length >= ids.length) {
    const keep = debts.find((d) => d.kind !== 'mortgage') ?? debts[0]!;
    excluded.splice(excluded.indexOf(keep.account_id), 1);
  }
  return { excluded, seen: ids };
}

/** Checks or unchecks a debt; null when that would leave nothing checked. */
export function toggleExcluded(excluded: number[], id: number, ids: number[]): number[] | null {
  if (excluded.includes(id)) return excluded.filter((x) => x !== id);
  const next = [...excluded, id];
  return ids.every((x) => next.includes(x)) ? null : next;
}

export const KIND_LABEL: Record<DebtKind, string> = { card: 'Credit cards', loan: 'Loans', mortgage: 'Mortgage' };

// ---------------------------------------------------------------- the plan panel

/** "Oct 2029", or words when the payments never catch up. */
export function debtFreeDate(plan: Pick<DebtPlanV2, 'never' | 'payoff_date'>): string {
  return plan.never || !plan.payoff_date ? 'Not yet' : monYear(plan.payoff_date);
}

/** The live line under the date. "Sooner" is always against today's minimum payments. */
export function planLine(plan: DebtPlanV2): string {
  if (plan.never) return 'With these payments the interest grows faster than the debts go down. Try a bigger extra amount.';
  const prefix = plan.extra > 0.004 ? `With ${money(plan.extra)} extra a month.` : 'At your normal payments, moving each finished payment to the next debt.';
  const base = plan.baseline;
  if (base.never) return `${prefix} At today’s minimum payments, some of it would never be paid off.`;
  const sooner = base.months !== null && plan.months !== null ? base.months - plan.months : 0;
  const saved = base.total_interest - plan.total_interest;
  if (sooner > 0 && base.payoff_date) return `${prefix} ${dur(sooner)} sooner than ${monYear(base.payoff_date)}, and about ${money(saved)} less interest.`;
  if (saved >= 1) return `${prefix} About ${money(saved)} less interest than at today’s minimum payments.`;
  return plan.extra > 0.004 ? prefix : 'At today’s minimum payments.';
}

/** The note under Most interest first / Smallest first. */
export function orderNote(plan: DebtPlanV2, order: 'avalanche' | 'snowball'): string {
  const diff = plan.compare.snowball.total_interest - plan.compare.avalanche.total_interest;
  if (order === 'avalanche') {
    return diff >= 1 ? `Saves the most money, about ${money(diff)} more than smallest first.` : 'Saves the most money. Here both ways cost about the same.';
  }
  return diff >= 1
    ? `You finish whole debts sooner, which can keep you going. It costs about ${money(diff)} more in interest.`
    : 'You finish whole debts sooner, which can keep you going. Here it costs about the same.';
}

// ---------------------------------------------------------------- Plan tab

export interface PlanRow {
  id: number;
  n: number;
  name: string;
  /** "$1,842 · 24.99% interest". */
  sub: string;
  /** "Oct 2027". */
  off: string;
  /** "was Mar 2031" (green) when the plan moves it earlier. */
  was: string | null;
  /** Bar widths, 0–100. */
  w: number;
  wasW: number;
}

/** "When each one is paid off", in plan order (where the extra goes first). */
export function planRows(plan: DebtPlanV2): { rows: PlanRow[]; endLabel: string } {
  const scale = Math.max(1, plan.months ?? 0, ...plan.debts.map((d) => d.baseline_months ?? 0), ...plan.debts.map((d) => d.payoff_months ?? 0));
  const pct = (m: number | null) => (m === null ? 100 : Math.max(2, (m / scale) * 100));
  const rows = plan.debts.map((d, i): PlanRow => {
    const moved = d.payoff_months !== null && (d.baseline_months === null || d.payoff_months < d.baseline_months);
    return {
      id: d.account_id,
      n: i + 1,
      name: d.name,
      sub: `${money(d.balance)} · ${d.apr_known ? `${aprText(d.apr)}% interest` : 'interest rate unknown'}`,
      off: d.payoff_date ? monYear(d.payoff_date) : 'Not with these payments',
      was: moved ? (d.baseline_date ? `was ${monYear(d.baseline_date)}` : 'never at today’s payments') : null,
      w: pct(d.payoff_months),
      wasW: pct(d.baseline_months),
    };
  });
  const endMonths = Math.max(...plan.debts.map((d) => d.baseline_months ?? d.payoff_months ?? 0), plan.months ?? 0);
  const firstMonth = plan.timeline[0]?.month;
  return { rows, endLabel: firstMonth ? monYear(addMonths(firstMonth, endMonths - 1)) : '' };
}

/** "What interest costs you". */
export function interestCells(plan: DebtPlanV2): { k: string; v: string; s: string }[] {
  return [
    { k: 'This month', v: money(plan.interest_this_month), s: 'in interest (an estimate)' },
    { k: 'Next 12 months', v: money(plan.interest_12), s: 'with your plan' },
    { k: 'Until debt-free', v: plan.never ? 'No end yet' : money(plan.total_interest), s: 'with your plan' },
  ];
}

export function interestLine(plan: DebtPlanV2): { text: string; good: boolean } {
  const base = plan.baseline;
  if (plan.never) return { text: 'With these payments the debts keep growing, so the interest never stops.', good: false };
  if (base.never) return { text: `At today’s minimum payments, some of it would never be paid off. Your plan pays it all with about ${money(plan.total_interest)} in interest.`, good: true };
  const saved = base.total_interest - plan.total_interest;
  if (saved >= 1) return { text: `At today’s minimum payments you’d pay ${money(base.total_interest)} in interest. Your plan saves about ${money(saved)}.`, good: true };
  return { text: 'Add an extra amount to see how much interest you’d save.', good: false };
}

/** Notes that keep the dates honest: unknown rates and cards. */
export function planCaveats(debts: Pick<DebtPlanDebt, 'apr_known' | 'kind' | 'name'>[]): string[] {
  const out: string[] = [];
  const unknown = debts.filter((d) => !d.apr_known);
  if (unknown.length === 1) out.push(`We don’t know ${unknown[0]!.name}’s interest rate, so it’s counted as 0% and its date may be too early.`);
  else if (unknown.length > 1) out.push(`We don’t know the interest rate for ${unknown.length} of these debts, so they’re counted as 0% and the dates may be too early.`);
  if (debts.some((d) => d.kind === 'card')) out.push('The dates assume no new charges on your cards.');
  return out;
}

// ---------------------------------------------------------------- Try it tab

export type Tone = 'good' | 'warn' | 'muted' | 'plain';

/** The one-time payment's result box. null while the server works it out. */
export function lumpView(plan: DebtPlanV2, amount: number | null): { head: string; body: string; good: boolean } | null {
  if (amount === null) return { head: 'Type an amount', body: 'See how a tax refund, bonus or gift would change your debt-free date.', good: false };
  const L = plan.lump;
  if (!L || Math.abs(L.amount - amount) > 0.004) return null;
  const names = L.paid_off.map((id) => plan.debts.find((d) => d.account_id === id)?.name).filter((n): n is string => !!n);
  const cleared = names.length ? ` ${names.join(' and ')} would be paid off right away.` : '';
  if (L.never || !L.payoff_date) return { head: 'Still not paid off', body: `The payments still don’t catch up with the interest.${cleared}`, good: false };
  const sooner = plan.months !== null && L.months !== null ? plan.months - L.months : 0;
  const saved = plan.total_interest - L.total_interest;
  const body =
    sooner > 0
      ? `${dur(sooner)} sooner than your plan, and about ${money(saved)} less interest.`
      : saved >= 1
        ? `The same month as your plan, with about ${money(saved)} less interest.`
        : 'About the same as your plan.';
  return { head: `Debt-free by ${monYear(L.payoff_date)}`, body: body + cleared, good: sooner > 0 || saved >= 1 };
}

/** round_cents(balance × apr ÷ 1200), half up (the server's rule), in cents. */
function interestCents(balanceCents: number, apr: number): number {
  if (balanceCents <= 0 || apr <= 0) return 0;
  return Math.floor((balanceCents * apr) / 1200 + 0.5);
}

/** One debt on its own at a fixed monthly payment: months to pay off (null = never) and the interest. */
export function payoffMonths(balance: number, apr: number, payment: number): { months: number | null; interest: number } {
  let b = Math.round(balance * 100);
  const pay = Math.round(payment * 100);
  if (b <= 0) return { months: 0, interest: 0 };
  if (pay <= interestCents(b, apr)) return { months: null, interest: 0 };
  let m = 0;
  let paid = 0;
  while (b > 0 && m < 600) {
    const i = interestCents(b, apr);
    paid += i;
    b += i;
    b -= Math.min(b, pay);
    m++;
  }
  return b > 0 ? { months: null, interest: paid / 100 } : { months: m, interest: paid / 100 };
}

/** "Try a different monthly payment": the line under one debt's field. `month` is today's 'YYYY-MM'. */
export function tryPaymentView(d: Pick<DebtPlanDebt, 'balance' | 'apr' | 'minimum'>, typed: string, month: string): { text: string; tone: Tone } {
  const n = parseMoney(typed);
  if (n === null) return { text: 'Type a monthly amount.', tone: 'muted' };
  if (n < d.minimum - 0.004) return { text: `The lowest payment for this one is ${money(d.minimum, !Number.isInteger(d.minimum))} a month.`, tone: 'warn' };
  const firstInterest = interestCents(Math.round(d.balance * 100), d.apr) / 100;
  if (n <= firstInterest) return { text: `That wouldn’t cover the interest, about ${money(firstInterest)} a month.`, tone: 'warn' };
  const r = payoffMonths(d.balance, d.apr, n);
  if (r.months === null) return { text: 'That wouldn’t pay it off within 50 years.', tone: 'warn' };
  const by = monYear(addMonths(month, r.months));
  const base = payoffMonths(d.balance, d.apr, d.minimum);
  if (base.months === null) return { text: `Paid off by ${by}. At today’s minimum payment it would never be paid off.`, tone: 'good' };
  const s = base.months - r.months;
  if (s > 0) return { text: `Paid off by ${by}. That’s ${dur(s)} sooner and about ${money(base.interest - r.interest)} less interest.`, tone: 'good' };
  return { text: `That’s about what you pay now. Paid off by ${by}.`, tone: 'plain' };
}

// ---------------------------------------------------------------- Progress tab

/** The headline under "Your debt going down" (never "12 months" until there are 12). */
export function progressHeadline(p: DebtProgress): { text: string; tone: Tone } {
  if (p.months.length < 2 || !p.since || p.change === null) {
    return {
      text: p.since
        ? `Iron Owl started tracking your debts in ${monthYearLong(p.since)}. Check back next month.`
        : 'Iron Owl doesn’t have past balances for these debts yet. Check back next month.',
      tone: 'plain',
    };
  }
  const since = monthYearLong(p.since);
  if (p.change < -0.5) return { text: `Went down by ${money(-p.change)} since ${since}`, tone: 'good' };
  if (p.change > 0.5) return { text: `Went up by ${money(p.change)} since ${since}`, tone: 'warn' };
  return { text: `About the same since ${since}`, tone: 'plain' };
}

export function progressCells(p: DebtProgress, plan: DebtPlanV2): { k: string; v: string; s: string; tone: Tone }[] {
  const out: { k: string; v: string; s: string; tone: Tone }[] = [];
  if (p.months.length >= 2 && p.since && p.change !== null) {
    const down = p.change <= 0;
    out.push({ k: down ? 'Paid down' : 'Went up', v: money(p.change), s: `since ${monYear(p.since)}`, tone: down ? (p.change < -0.5 ? 'good' : 'plain') : 'warn' });
  }
  const owed = plan.debts.reduce((s, d) => s + d.balance, 0);
  const n = plan.debts.length;
  out.push({ k: 'Owe now', v: money(owed), s: n === 1 ? '1 debt included' : `${n} debts included`, tone: 'plain' });
  const base = plan.baseline;
  const sooner = !plan.never && !base.never && base.months !== null && plan.months !== null ? base.months - plan.months : 0;
  out.push({
    k: 'Debt-free with your plan',
    v: debtFreeDate(plan),
    s: plan.never
      ? 'the payments don’t catch up yet'
      : base.never
        ? 'today’s payments would never pay it all off'
        : sooner > 0
          ? `${dur(sooner)} sooner than at today’s payments`
          : 'at today’s payments',
    tone: 'plain',
  });
  return out;
}

/** "Card limit used": the note under one card. */
export function cardView(c: DebtProgress['cards'][number]): { util: string | null; pct: number | null; note: string; tone: Tone } {
  if (c.limit === null || c.used_pct === null || c.limit <= 0) return { util: null, pct: null, note: 'Your bank didn’t share this card’s limit.', tone: 'muted' };
  const pct = Math.max(0, Math.round(c.used_pct));
  if (c.used_pct > 30) return { util: `${pct}%`, pct, note: `Over 30%. Paying it down to ${money(c.limit * 0.3)} would get it under.`, tone: 'warn' };
  return { util: `${pct}%`, pct, note: 'Under 30%. That helps your credit score.', tone: 'plain' };
}

/** 1, 2, 2.5 or 5 × 10ⁿ at or above v. */
export function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const k of [1, 2, 2.5, 5, 10]) if (k * p >= v - 1e-9) return k * p;
  return 10 * p;
}

export interface ChartModel {
  /** SVG paths in a 1000 × 220 box. */
  past: string | null;
  area: string | null;
  plan: string;
  base: string;
  /** Where "Now" sits, 0–1000. */
  nowX: number;
  top: number;
  startLabel: string | null;
  nowLabel: string;
  endLabel: string;
  estimated: boolean;
  /** The baseline never reaches zero inside the chart. */
  baseNever: boolean;
}

/**
 * "Your debt going down": the real months on the left quarter (only months where every
 * included debt has a balance), then the plan (dashed) and today's minimum payments (dashed
 * gray) from Now to the end. Without two real months there's no left part.
 */
export function chartModel(p: DebtProgress, plan: DebtPlanV2, month: string): ChartModel {
  const hasPast = p.months.length >= 2;
  const nowX = hasPast ? 250 : 0;
  const owed = plan.debts.reduce((s, d) => s + d.balance, 0);
  const finite = [plan.never ? null : plan.months, plan.baseline.never ? null : plan.baseline.months].filter((m): m is number => m !== null);
  let end = finite.length ? Math.max(...finite) : Math.min(600, Math.max(plan.timeline.length, plan.baseline.timeline.length));
  if (plan.baseline.never && !plan.never && plan.months !== null) end = Math.min(600, Math.max(end, Math.ceil(plan.months * 1.5)));
  end = Math.max(1, end);
  const planVals = [owed, ...plan.timeline.slice(0, end).map((t) => t.total)];
  const baseVals = [owed, ...plan.baseline.timeline.slice(0, end).map((t) => t.total)];
  const pastVals = hasPast ? p.months.map((m) => m.total) : [];
  const top = niceCeil(Math.max(1, ...planVals, ...baseVals, ...pastVals));
  const y = (v: number) => (220 - (Math.max(0, v) / top) * 220).toFixed(1);
  const xf = (k: number) => (nowX + (k / end) * (1000 - nowX)).toFixed(1);
  const line = (vals: number[]) => 'M' + vals.map((v, k) => `${xf(k)},${y(v)}`).join(' L');
  let past: string | null = null;
  let area: string | null = null;
  if (hasPast) {
    const n = p.months.length;
    const pts = pastVals.map((v, i) => `${((i / (n - 1)) * 250).toFixed(1)},${y(v)}`);
    past = 'M' + pts.join(' L');
    area = `M${pts.join(' L')} L250.0,220 L0,220 Z`;
  }
  return {
    past,
    area,
    plan: line(planVals),
    base: line(baseVals),
    nowX,
    top,
    startLabel: hasPast && p.since ? monYear(p.since) : null,
    nowLabel: monYear(month),
    endLabel: monYear(addMonths(month, end)),
    estimated: p.months.some((m) => m.estimated),
    baseNever: plan.baseline.never,
  };
}
