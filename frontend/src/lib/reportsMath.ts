/**
 * Reports v2 › Overview: the numbers and the words (reports-v2 design notes, Screen 1; SPEC
 * "Reports v2 ... (Release 3.14)"). Everything reads one `api.reports.monthly(n)` response whose
 * last month is the current one, in progress: FETCH_MONTHS months at first, more (monthsToFetch)
 * when the One month picker goes further back. The charts always show the last SHOWN_MONTHS. Kept free of runtime imports so
 * `npm run test:unit` (scripts/reportsOverview.test.ts) can load it directly; `lib/reports.ts`
 * re-exports the shared pieces.
 */
import type { MonthlyReport, MonthlyReportCategory } from '../api';

/** Months Money in and out and the trend chart show (the last ones). */
export const SHOWN_MONTHS = 6;
/** Full months before a month that make up its "usual" (the average). */
export const USUAL_MONTHS = 3;
/** Months the Overview asks for first: the shown ones plus the usual months before the first of them. */
export const FETCH_MONTHS = SHOWN_MONTHS + USUAL_MONTHS;
/** The most months `GET /api/reports/monthly` returns (backend `reports.MAX_MONTHS`). */
export const MAX_REPORT_MONTHS = 60;
/** Larger fetches grow by whole years, so stepping back one month at a time doesn't refetch each time. */
const FETCH_STEP = 12;

export type Period = 'month' | 'q' | 'h';
export const isPeriod = (v: unknown): v is Period => v === 'month' || v === 'q' || v === 'h';

/** Where today falls in the current month. */
export interface Today {
  month: string; // YYYY-MM
  day: number;
  daysInMonth: number;
  /** day / daysInMonth. */
  fraction: number;
}

export function todayInfo(now = new Date()): Today {
  const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  return { month, day: now.getDate(), daysInMonth, fraction: now.getDate() / daysInMonth };
}

export function daysInMonthOf(month: string): number {
  const [y, m] = month.split('-').map(Number) as [number, number];
  return new Date(y, m, 0).getDate();
}

/** Amounts under this are rounding dust, not money. */
export const DUST = 0.004;
export const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

// ---------------------------------------------------------------- words

const usd0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
/** "$1,234", never a sign (the words say which way). */
export const W = (v: number) => usd0.format(Math.round(Math.abs(v)));
/** "September". */
export function monthWord(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  return new Date(y, m - 1, 1).toLocaleString('en-US', { month: 'long' });
}
/** "Sep". */
export function monthAbbr(month: string): string {
  const [y, m] = month.split('-').map(Number) as [number, number];
  return new Date(y, m - 1, 1).toLocaleString('en-US', { month: 'short' });
}
export const yearOf = (month: string) => month.slice(0, 4);

// ---------------------------------------------------------------- basic series

/** What a category spent in month i (spending and fixed categories alike). */
export function spentIn(report: MonthlyReport, catId: string, i: number): number {
  const m = report.months[i];
  if (!m) return 0;
  return m.by_category[catId] ?? m.by_fixed[catId] ?? 0;
}

/** The category's budget plan in month i (0 without one). */
export function plannedIn(report: MonthlyReport, catId: string, i: number): number {
  return report.months[i]?.planned?.[catId] ?? 0;
}

/** The category's spending, one number per report month (oldest first). */
export function monthlySeries(report: MonthlyReport, catId: string): number[] {
  return report.months.map((_, i) => spentIn(report, catId, i));
}

/**
 * Index of the first month with any activity. Months before your history began are left
 * out of averages so they don't drag "usual" toward $0.
 */
export function firstActiveIndex(report: MonthlyReport): number {
  const i = report.months.findIndex((m) => Math.abs(m.income) + Math.abs(m.spending) + Math.abs(m.fixed) > DUST);
  return i < 0 ? report.months.length - 1 : i;
}

export const isCurrentIndex = (report: MonthlyReport, i: number, today: Today) => report.months[i]?.month === today.month;

/**
 * A month's figure projected to the whole month: the current month is scaled by
 * day ÷ days in month; bills and earlier months are taken as they are.
 */
export function paceOf(value: number, i: number, report: MonthlyReport, today: Today, bill: boolean): number {
  if (!isCurrentIndex(report, i, today) || bill || today.fraction <= 0) return value;
  return value / today.fraction;
}

/** Indexes of the full months (at most USUAL_MONTHS, none before your history began) before month i. */
export function usualMonthsAt(report: MonthlyReport, i: number): number[] {
  const from = Math.max(firstActiveIndex(report), i - USUAL_MONTHS, 0);
  const out: number[] = [];
  for (let j = from; j < i; j++) out.push(j);
  return out;
}

/** A category's usual month before month i: the average of the months before it; null without any. */
export function usualAt(report: MonthlyReport, catId: string, i: number): number | null {
  const months = usualMonthsAt(report, i);
  if (!months.length) return null;
  return sum(months.map((j) => spentIn(report, catId, j))) / months.length;
}

/** "June to August" / "August" for the usual months before month i, or null. */
export function usualLabelAt(report: MonthlyReport, i: number): string | null {
  const months = usualMonthsAt(report, i);
  if (!months.length) return null;
  const a = monthWord(report.months[months[0]!]!.month);
  const b = monthWord(report.months[months[months.length - 1]!]!.month);
  return months.length === 1 ? a : `${a} to ${b}`;
}

// ---------------------------------------------------------------- the period

export interface View {
  period: Period;
  /** Indexes into report.months the period covers. */
  idx: number[];
  /** One month, and it's the current month (pace, "so far", action buttons). */
  isCur: boolean;
  /** The period includes the current month. */
  hasCur: boolean;
  /** One month: its index. Otherwise the last index. */
  at: number;
  /** "September" / "Last 3 months". */
  label: string;
}

/** 'YYYY-MM' moved by n months. */
export function shiftMonth(month: string, n: number): string {
  const k = Number(month.slice(0, 4)) * 12 + Number(month.slice(5, 7)) - 1 + n;
  return `${Math.floor(k / 12)}-${String((k % 12) + 1).padStart(2, '0')}`;
}

/** Months from a to b ('YYYY-MM'): positive when b is later. */
const monthDiff = (a: string, b: string) => Number(b.slice(0, 4)) * 12 + Number(b.slice(5, 7)) - (Number(a.slice(0, 4)) * 12 + Number(a.slice(5, 7)));

/**
 * The months the One month picker offers, oldest first, ending with the report's last month:
 * back to the first month with data (`first_month`), but no further than the fetch limit leaves
 * room for that month's usual months. When the loaded months reach that far, months at the start
 * without any money in or out are skipped too (as the old stepper did).
 */
export function pickerMonths(report: MonthlyReport): string[] {
  const n = report.months.length;
  if (!n) return [];
  const newest = report.months[n - 1]!.month;
  const floor = shiftMonth(newest, -(MAX_REPORT_MONTHS - 1 - USUAL_MONTHS));
  const active = report.months[Math.min(firstActiveIndex(report), n - 1)]!.month;
  let oldest = report.first_month ?? active;
  if (oldest < floor) oldest = floor;
  if (report.months[0]!.month <= oldest && active > oldest) oldest = active;
  if (oldest > newest) oldest = newest;
  const out: string[] = [];
  for (let m = oldest; m <= newest; m = shiftMonth(m, 1)) out.push(m);
  return out;
}

/** `month` if the list has it, else the nearest end of the list (months: oldest first). */
export function clampMonth(month: string, months: string[]): string {
  const first = months[0]!;
  const last = months[months.length - 1]!;
  return month < first ? first : month > last ? last : month;
}

/**
 * How many months to ask `GET /api/reports/monthly` for (ending with `newest`) so `month` and its
 * usual months are loaded: FETCH_MONTHS when they fit, else grown by whole years, at most
 * MAX_REPORT_MONTHS.
 */
export function monthsToFetch(month: string, newest: string): number {
  const need = monthDiff(month, newest) + 1 + USUAL_MONTHS;
  if (need <= FETCH_MONTHS) return FETCH_MONTHS;
  return Math.min(MAX_REPORT_MONTHS, Math.ceil(need / FETCH_STEP) * FETCH_STEP);
}

/** The view; `monthIndex` (One month) is an index into report.months. */
export function viewOf(report: MonthlyReport, period: Period, monthIndex: number, today: Today): View {
  const last = report.months.length - 1;
  if (period === 'month') {
    const at = Math.min(last, Math.max(0, monthIndex));
    const isCur = isCurrentIndex(report, at, today);
    return { period, idx: [at], isCur, hasCur: isCur, at, label: monthWord(report.months[at]!.month) };
  }
  const count = Math.min(period === 'q' ? 3 : 6, report.months.length);
  const idx = Array.from({ length: count }, (_, k) => last - count + 1 + k);
  return { period, idx, isCur: false, hasCur: idx.some((i) => isCurrentIndex(report, i, today)), at: last, label: `Last ${count} months` };
}

/** A category's numbers for the period. */
export interface CatFig {
  cat: MonthlyReportCategory;
  spent: number;
  /** spent with the current month projected to a full month. */
  paced: number;
  plan: number;
  /** One month: the usual month. Last 3 months: the 3 months before. 6 months or no history: null. */
  usual: number | null;
}

export function figureOf(report: MonthlyReport, cat: MonthlyReportCategory, v: View, today: Today): CatFig {
  const spent = sum(v.idx.map((i) => spentIn(report, cat.id, i)));
  const paced = sum(v.idx.map((i) => paceOf(spentIn(report, cat.id, i), i, report, today, cat.bill)));
  const plan = sum(v.idx.map((i) => plannedIn(report, cat.id, i)));
  let usual: number | null = null;
  if (v.period === 'month') usual = usualAt(report, cat.id, v.at);
  else if (v.period === 'q') {
    // The 3 months before, only when all of them are inside your history.
    const first = v.idx[0]! - v.idx.length;
    if (first >= 0 && first >= firstActiveIndex(report)) usual = sum(v.idx.map((i) => spentIn(report, cat.id, i - v.idx.length)));
  }
  return { cat, spent, paced, plan, usual };
}

export function figuresOf(report: MonthlyReport, v: View, today: Today): CatFig[] {
  return report.categories.map((c) => figureOf(report, c, v, today));
}

/** Total money in and out for the period. Out counts everything, bills included. */
export interface Money {
  income: number;
  out: number;
  /** out with the current month at its pace. */
  outPaced: number;
  /** income − out. */
  leftNow: number;
  /** income − outPaced (what's left at the end of the month at this pace). */
  leftEnd: number;
}

/** Money out of month i at its pace: non-bill categories scale with the month, the rest stays. */
export function outPacedIn(report: MonthlyReport, i: number, today: Today): number {
  const m = report.months[i]!;
  const out = m.spending + m.fixed;
  if (!isCurrentIndex(report, i, today)) return out;
  const extra = sum(report.categories.filter((c) => !c.bill).map((c) => paceOf(spentIn(report, c.id, i), i, report, today, false) - spentIn(report, c.id, i)));
  return out + extra;
}

export function moneyOf(report: MonthlyReport, v: View, today: Today): Money {
  const income = sum(v.idx.map((i) => report.months[i]!.income));
  const out = sum(v.idx.map((i) => report.months[i]!.spending + report.months[i]!.fixed));
  const outPaced = sum(v.idx.map((i) => outPacedIn(report, i, today)));
  return { income, out, outPaced, leftNow: income - out, leftEnd: income - outPaced };
}

// ---------------------------------------------------------------- Running higher or lower

export interface Change {
  fig: CatFig;
  /** The period's amount compared (pace when it includes the current month). */
  base: number;
  /** base − usual; positive = higher. */
  diff: number;
}

/** Differences under this many dollars don't count as "running higher or lower". */
export const CHANGE_MIN = 10;

/** Top 4 non-bill categories by |pace − usual|, ignoring differences under $10. */
export function changesOf(figs: CatFig[], limit = 4): Change[] {
  return figs
    .filter((f) => !f.cat.bill && f.usual !== null)
    .map((f) => ({ fig: f, base: f.paced, diff: f.paced - f.usual! }))
    .filter((x) => Math.abs(x.diff) >= CHANGE_MIN)
    .sort((a, b) => Math.abs(b.diff) - Math.abs(a.diff))
    .slice(0, limit);
}

/** The words for one Higher / Lower item. */
export function changeWords(x: Change, v: View): { tag: 'Higher' | 'Lower'; title: string; sub: string } {
  const up = x.diff > 0;
  return {
    tag: up ? 'Higher' : 'Lower',
    title: `${x.fig.cat.name}: ${W(x.diff)} ${up ? 'more' : 'less'} than usual`,
    sub: v.hasCur ? `On pace for ${W(x.fig.paced)} · usually ${W(x.fig.usual!)}` : `Spent ${W(x.fig.spent)} · usually ${W(x.fig.usual!)}`,
  };
}

/** The section's subtitle (null for 6 months) and its empty sentence. */
export function changesText(report: MonthlyReport, v: View, figs: CatFig[]): { sub: string | null; none: string } {
  const noHist = figs.filter((f) => !f.cat.bill).every((f) => f.usual === null);
  const none =
    v.period === 'h'
      ? 'Pick one month or the last 3 months to see what’s changing.'
      : noHist
        ? 'Once a few months are behind you, this shows what’s running higher or lower.'
        : 'Nothing stands out. Every category is close to its usual amount.';
  let sub: string | null = null;
  if (v.period === 'month' && !noHist) {
    const name = monthWord(report.months[v.at]!.month);
    const range = usualLabelAt(report, v.at);
    sub = v.isCur
      ? `${name}’s pace compared with a usual month${range ? ` (${range})` : ''}. Bills are left out.`
      : `${name} compared with the ${usualMonthsAt(report, v.at).length === 1 ? 'month' : `${usualMonthsAt(report, v.at).length} months`} before. Bills are left out.`;
  } else if (v.period === 'q' && !noHist) sub = 'Compared with the 3 months before. Bills are left out.';
  return { sub, none };
}

// ---------------------------------------------------------------- the summary card

export interface SummaryInput {
  v: View;
  /** report.months[v.at].month */
  month: string;
  hideBills: boolean;
  /** Visible spending (bills left out when hidden). */
  spent: number;
  paced: number;
  /** Visible usual, or null without a comparison. */
  usual: number | null;
  top: Change | null;
  money: Money;
}

/** Within 2% of usual counts as "about the same". */
export const isAboutSame = (d: number, usual: number) => Math.abs(d) < Math.abs(usual) * 0.02;

export function summaryText(s: SummaryInput): { lead: string; rest: string } {
  const { v, hideBills, spent, paced, usual, top, money } = s;
  const name = monthWord(s.month);
  const what = hideBills ? ' on day-to-day spending' : '';
  const where = v.period === 'month' ? `in ${name}` : v.period === 'q' ? 'over the last 3 months' : 'over the last 6 months';
  const lead = v.isCur ? `You’ve spent ${W(spent)}${what} in ${name} so far.` : `You spent ${W(spent)}${what} ${where}.`;

  const parts: string[] = [];
  if (usual !== null) {
    const d = paced - usual;
    const than = v.period === 'q' ? 'the 3 months before' : 'usual';
    if (isAboutSame(d, usual)) parts.push(v.isCur ? 'That’s on pace to be about the same as usual.' : `That’s about the same as ${than}.`);
    else parts.push(v.isCur ? `That’s on pace to be ${W(d)} ${d > 0 ? 'more' : 'less'} than usual.` : `That’s ${W(d)} ${d > 0 ? 'more' : 'less'} than ${than}.`);
  }
  if (top) {
    const n = top.fig.cat.name;
    if (top.diff > 0) parts.push(`${n} ${v.isCur ? 'is running' : 'ran'} ${W(top.diff)} higher than usual.`);
    else parts.push(`${n} ${v.isCur ? 'is' : 'was'} ${W(top.diff)} lower than usual.`);
  }
  if (v.isCur) {
    parts.push(
      money.leftEnd >= 0
        ? `At this pace, you’ll end the month with about ${W(money.leftEnd)} left over.`
        : `At this pace, you’ll spend about ${W(money.leftEnd)} more than you earn this month.`,
    );
  } else if (money.leftNow < -DUST) parts.push(`You spent ${W(money.leftNow)} more than you earned.`);
  else if (v.period === 'month') parts.push(`You had ${W(money.leftNow)} left over.`);
  else parts.push(`You had ${W(money.leftNow)} left over, about ${W(money.leftNow / v.idx.length)} a month.`);
  return { lead, rest: parts.join(' ') };
}

// ---------------------------------------------------------------- the three stat cards

export interface StatCard {
  key: 'spent' | 'usual' | 'left';
  label: string;
  value: string;
  sub: string;
  /** Hue of the card's tint. */
  hue: number;
  /** Amber value text (a shortfall). */
  warn: boolean;
}

export function statCards(s: SummaryInput & { plan: number; today: Today }): StatCard[] {
  const { v, hideBills, spent, paced, usual, money, plan } = s;
  const bills = hideBills ? 'bills hidden' : 'bills included';
  const spentCard: StatCard = {
    key: 'spent',
    label: v.isCur ? 'Spent so far' : 'Spent',
    value: W(spent),
    sub: plan > DUST ? `of ${W(plan)} planned · ${bills}` : `No plan · ${bills}`,
    hue: 268,
    warn: false,
  };
  let uv: string;
  let us: string;
  if (usual === null) {
    uv = v.period === 'h' ? 'No comparison' : 'Not enough history';
    us = v.period === 'h' ? 'Pick one month or 3 months to compare' : 'Shows once you have 3 months of history';
  } else {
    const d = paced - usual;
    uv = isAboutSame(d, usual) ? 'About the same' : `${W(d)} ${d > 0 ? 'more' : 'less'}`;
    us = v.hasCur ? `On pace for ${W(paced)} · usually ${W(usual)}` : `Usually ${W(usual)}`;
  }
  const usualCard: StatCard = { key: 'usual', label: 'Compared with usual', value: uv, sub: us, hue: 200, warn: false };

  const lv = v.isCur ? money.leftEnd : money.leftNow;
  const short = lv < -DUST;
  const pct = money.income > DUST ? Math.round((lv / money.income) * 100) : null;
  let ls: string;
  if (v.isCur) {
    const now = money.leftNow >= -DUST ? `${W(money.leftNow)} left right now` : `${W(money.leftNow)} short right now`;
    ls = pct !== null && !short ? `${now} · ${pct}% of income` : now;
  } else if (short) ls = 'Spent more than you earned';
  else ls = pct !== null ? `${pct}% of your ${W(money.income)} income` : 'No income in this period';
  const leftCard: StatCard = {
    key: 'left',
    label: v.isCur ? `Left over by ${monthAbbr(s.month)} ${s.today.daysInMonth}` : 'Left over',
    value: short ? `${W(lv)} short` : `${v.isCur ? 'About ' : ''}${W(lv)}`,
    sub: ls,
    hue: short ? 80 : 155,
    warn: short,
  };
  return [spentCard, usualCard, leftCard];
}

// ---------------------------------------------------------------- Plan vs. what you spent

export interface PlanStatus {
  /** Amber words: over, or likely to go over. */
  warn: boolean;
  text: string;
}

/** The status line under a plan row. */
export function planStatus(spent: number, paced: number, plan: number, isCur: boolean): PlanStatus {
  if (plan <= DUST) return { warn: false, text: spent > DUST ? 'No plan for this' : 'Nothing planned or spent' };
  const over = spent > plan + 0.5;
  const likely = isCur && !over && paced > plan + 0.5;
  if (over) return { warn: true, text: `Over plan by ${W(spent - plan)}` };
  if (likely) return { warn: true, text: `Likely to go over by ${W(paced - plan)}` };
  return { warn: false, text: isCur ? `On track · ${W(plan - spent)} left` : `${W(plan - spent)} under plan` };
}

export interface PlanRow {
  key: string;
  name: string;
  hue: number;
  bill: boolean;
  /** A group row (opens its categories). */
  group: boolean;
  spent: number;
  paced: number;
  plan: number;
  status: PlanStatus;
  /** Bar widths in % of the plan (0 without a plan). */
  fill: number;
  ghost: number;
}

export function planRow(key: string, name: string, hue: number, figs: CatFig[], group: boolean, isCur: boolean): PlanRow {
  const spent = sum(figs.map((f) => f.spent));
  const paced = sum(figs.map((f) => f.paced));
  const plan = sum(figs.map((f) => f.plan));
  const pct = (x: number) => (plan > DUST ? Math.min(100, (x / plan) * 100) : 0);
  return {
    key,
    name,
    hue,
    bill: !group && figs.every((f) => f.cat.bill),
    group,
    spent,
    paced,
    plan,
    status: planStatus(spent, paced, plan, isCur),
    fill: pct(spent),
    ghost: isCur ? pct(paced) : 0,
  };
}

/** "Name: $471 spent of $650 planned. On track · $179 left." */
export function planRowLabel(r: PlanRow): string {
  const money = r.plan > DUST ? `${W(r.spent)} spent of ${W(r.plan)} planned` : `${W(r.spent)} spent, no plan`;
  return `${r.name}${r.bill ? ', bill' : ''}: ${money}. ${r.status.text}.${r.group ? ' Show its categories.' : ''}`;
}

/** "$822 of $1,030" (or just "$822" without a plan). */
export const planTotalText = (spent: number, plan: number) => (plan > DUST ? `${W(spent)} of ${W(plan)}` : W(spent));

// ---------------------------------------------------------------- the category panel's note

export type NoteTone = 'warn' | 'good' | '';

/** The note box in the category panel (thresholds: ±8% of usual). */
export function categoryNote(
  f: CatFig,
  v: View,
  month: string,
  topMerchant: string | undefined,
): { tone: NoteTone; title: string; body: string } {
  if (f.cat.bill) {
    return { tone: '', title: 'Fixed bill', body: 'This amount doesn’t change with habits. Check once a year whether a cheaper plan or rate is available.' };
  }
  const u = f.usual;
  if (u === null) return { tone: '', title: 'Not enough history yet', body: 'Once a few months are behind you, this compares each month with your usual amount.' };
  const b = f.paced;
  if (u <= DUST) {
    if (b <= DUST) return { tone: '', title: 'Steady', body: 'Nothing spent here, same as usual.' };
    return { tone: 'warn', title: 'New this period', body: `You don’t usually spend here. ${v.isCur ? `At this pace ${monthWord(month)} ends near` : 'That comes to'} ${W(b)}.` };
  }
  if (b > u * 1.08) {
    const lead = v.isCur ? `At this pace ${monthWord(month)} ends near` : v.period === 'month' ? `${monthWord(month)} ended at` : 'That comes to';
    const look = topMerchant ? ` ${topMerchant} is the biggest share, so that’s the first place to look.` : '';
    return { tone: 'warn', title: `Running ${Math.round((b / u - 1) * 100)}% above usual`, body: `${lead} ${W(b)}.${look}` };
  }
  if (b < u * 0.92) {
    return { tone: 'good', title: `${Math.round((1 - b / u) * 100)}% below usual`, body: `That’s ${W(u - b)} less than usual. If it holds, you could move the difference to savings.` };
  }
  return { tone: '', title: 'Steady', body: `Close to your usual ${W(u)}. No change needed.` };
}

// ---------------------------------------------------------------- Money in and out

export interface CashColumn {
  month: string;
  income: number;
  out: number;
  /** out at its pace (the current month), else out. */
  paced: number;
  /** income − paced. */
  left: number;
  current: boolean;
  inPeriod: boolean;
  /** "$2,105 left" / "about $1,788 left" / "$X short". */
  leftText: string;
}

/** The last SHOWN_MONTHS months, oldest first. */
export function cashColumns(report: MonthlyReport, v: View, today: Today): CashColumn[] {
  const n = report.months.length;
  const from = Math.max(0, n - SHOWN_MONTHS);
  const out: CashColumn[] = [];
  for (let i = from; i < n; i++) {
    const m = report.months[i]!;
    const current = isCurrentIndex(report, i, today);
    const o = m.spending + m.fixed;
    const paced = outPacedIn(report, i, today);
    const left = m.income - paced;
    out.push({
      month: m.month,
      income: m.income,
      out: o,
      paced,
      left,
      current,
      inPeriod: v.idx.includes(i),
      leftText: left >= -DUST ? `${current ? 'about ' : ''}${W(left)} left` : `${W(left)} short`,
    });
  }
  return out;
}

export function cashLabel(cols: CashColumn[]): string {
  return `Money in and out by month: ${cols.map((c) => `${monthAbbr(c.month)} in ${W(c.income)}, out ${W(c.out)}`).join('; ')}.`;
}
