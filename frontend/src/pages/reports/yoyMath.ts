/**
 * Reports › Year over year (Release 3.14): the words, rows and sorting worked out on the client
 * from `GET /api/reports/yoy`. Kept free of runtime imports so `npm run test:unit` can load it
 * directly (scripts/reportsHabits.test.ts).
 */
import type { YoyMode, YoyReport, YoySide } from '../../api';

const fmt0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 });
const fmt2 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
/** "$1,234" (never a sign: the words say which way). */
export const money = (v: number) => fmt0.format(Math.abs(v));
/** "$34.52" */
export const money2 = (v: number) => fmt2.format(Math.abs(v));

const LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const mi = (m: string) => Number(m.slice(5, 7)) - 1;
/** '2026-09' or '2026-09-22' → 'September'. */
export const monthLong = (m: string) => LONG[mi(m)] ?? m;
/** '2026-09' → 'Sep'. */
export const monthAbbr = (m: string) => SHORT[mi(m)] ?? m;
const dayOf = (iso: string) => Number(iso.slice(8, 10));
export const yearOf = (side: YoySide) => side.start.slice(0, 4);

/** Changes under this share (2%) of last year's amount read as "the same". */
export const SAME_SHARE = 0.02;

function ordinal(n: number): string {
  const t = n % 100;
  if (t >= 11 && t <= 13) return `${n}th`;
  return `${n}${n % 10 === 1 ? 'st' : n % 10 === 2 ? 'nd' : n % 10 === 3 ? 'rd' : 'th'}`;
}

const visits = (n: number) => `${n.toLocaleString('en-US')} ${n === 1 ? 'visit' : 'visits'}`;

// ---------------------------------------------------------------- toggle and empty state

export const modeLabel = (mode: YoyMode) => (mode === 'year' ? 'This year so far' : 'One month');

/** A finished month (Release 3.18): the whole month vs the whole same month last year. */
export const isWholeMonth = (r: YoyReport) => r.mode === 'month' && !r.partial;

/** '2026-03' → 'March 2025' (the same month a year before). */
const monthLastYear = (m: string) => `${monthLong(m)} ${Number(m.slice(0, 4)) - 1}`;

/**
 * The note next to the toggle. This year / this month: "compared with the same days in 2025. Same
 * days both years, so it’s a fair comparison." A finished month: "compared with all of March 2025."
 */
export function compareNote(todayMonth: string, month?: string | null): string {
  if (month && month < todayMonth) return `compared with all of ${monthLastYear(month)}.`;
  return `compared with the same days in ${Number(todayMonth.slice(0, 4)) - 1}. Same days both years, so it’s a fair comparison.`;
}

/** No spending last year (SPEC: previous.total is 0): the tab shows one EmptyState. */
export const isEmpty = (r: YoyReport) => r.previous.total <= 0.004;

export function emptyText(firstMonth: string | null): string {
  const base = 'Year over year fills in once you have a year of history.';
  return firstMonth ? `${base} You have data from ${monthLong(firstMonth)} ${firstMonth.slice(0, 4)}.` : base;
}

// ---------------------------------------------------------------- comparing two amounts

export type Trend = 'up' | 'down' | 'same';

export interface Change {
  trend: Trend;
  /** "Higher" / "Lower" / "Same" */
  tag: string;
  /** "$450 more (+3%)" / "$330 less (−15%)" / "About the same" */
  words: string;
}

/** Last year's amount vs this year's, in words. Within 2% (or under $1) is the same. */
export function change(prev: number, cur: number): Change {
  const d = cur - prev;
  if (Math.abs(d) < 1 || (prev > 0.004 && Math.abs(d) / prev < SAME_SHARE)) return { trend: 'same', tag: 'Same', words: 'About the same' };
  const pct = prev > 0.004 ? Math.round((Math.abs(d) / prev) * 100) : null;
  if (d > 0) return { trend: 'up', tag: 'Higher', words: `${money(d)} more${pct === null ? ' (new this year)' : ` (+${pct}%)`}` };
  return { trend: 'down', tag: 'Lower', words: `${money(d)} less${pct === null ? '' : ` (−${pct}%)`}` };
}

// ---------------------------------------------------------------- summary and stats

/** "January 1 to September 22" / "September 1 to 22" / "September 1". */
export function spanText(side: YoySide): string {
  const a = `${monthLong(side.start)} ${dayOf(side.start)}`;
  if (side.start === side.end) return a;
  if (side.start.slice(0, 7) === side.end.slice(0, 7)) return `${a} to ${dayOf(side.end)}`;
  return `${a} to ${monthLong(side.end)} ${dayOf(side.end)}`;
}

/** The summary card's first sentence. A finished month from an earlier year than `thisYear`
 * ('YYYY') names its year: "In March 2025 you spent …". */
export function leadSentence(r: YoyReport, thisYear?: string): string {
  const cur = r.current.total;
  const c = change(r.previous.total, cur);
  if (r.mode === 'year') {
    const tail = c.trend === 'same' ? 'about the same as by this time last year' : `${money(cur - r.previous.total)} ${c.trend === 'up' ? 'more' : 'less'} than by this time last year`;
    return `So far this year you’ve spent ${money(cur)}, ${tail}.`;
  }
  if (isWholeMonth(r)) {
    const last = monthLastYear(r.current.start);
    const tail = c.trend === 'same' ? `about the same as ${last}` : `${money(cur - r.previous.total)} ${c.trend === 'up' ? 'more' : 'less'} than ${last}`;
    const year = r.current.start.slice(0, 4);
    const name = thisYear && year !== thisYear ? `${monthLong(r.current.start)} ${year}` : monthLong(r.current.start);
    return `In ${name} you spent ${money(cur)}, ${tail}.`;
  }
  const when = r.current.start === r.current.end ? `On ${spanText(r.current)}` : `From ${spanText(r.current)}`;
  const tail = c.trend === 'same' ? 'about the same as the same days last year' : `${money(cur - r.previous.total)} ${c.trend === 'up' ? 'more' : 'less'} than the same days last year`;
  return `${when} you spent ${money(cur)}, ${tail}.`;
}

export interface CategoryRow {
  id: string;
  name: string;
  hue: number;
  bill: boolean;
  prev: number;
  cur: number;
  change: Change;
}

/** Every category spent in either year, sorted by this year's amount (then last year's). */
export function categoryRows(r: YoyReport): CategoryRow[] {
  const map = new Map<string, CategoryRow>();
  const add = (side: YoySide, key: 'prev' | 'cur') => {
    for (const c of side.categories) {
      const row = map.get(c.id) ?? { id: c.id, name: c.name, hue: c.hue, bill: c.bill, prev: 0, cur: 0, change: change(0, 0) };
      row[key] += c.total;
      map.set(c.id, row);
    }
  };
  add(r.current, 'cur');
  add(r.previous, 'prev');
  const rows = [...map.values()];
  for (const row of rows) row.change = change(row.prev, row.cur);
  return rows.sort((a, b) => b.cur - a.cur || b.prev - a.prev || a.name.localeCompare(b.name));
}

export interface StoreRow {
  name: string;
  /** The category's name ("" when unknown). */
  category: string;
  prev: number;
  cur: number;
  prevCount: number;
  curCount: number;
  change: Change;
}

/** Every store from either year, sorted by this year's spending (then last year's). */
export function storeRows(r: YoyReport): StoreRow[] {
  const catName = new Map<string, string>();
  for (const c of r.previous.categories) catName.set(c.id, c.name);
  for (const c of r.current.categories) catName.set(c.id, c.name);
  const map = new Map<string, StoreRow>();
  const add = (side: YoySide, cur: boolean) => {
    for (const m of side.merchants) {
      const row = map.get(m.name) ?? { name: m.name, category: '', prev: 0, cur: 0, prevCount: 0, curCount: 0, change: change(0, 0) };
      if (cur) {
        row.cur += m.total;
        row.curCount += m.count;
      } else {
        row.prev += m.total;
        row.prevCount += m.count;
      }
      // This year's category wins; last year's fills in for a store not visited this year.
      if (cur || !row.category) row.category = catName.get(m.category) ?? row.category;
      map.set(m.name, row);
    }
  };
  add(r.current, true);
  add(r.previous, false);
  const rows = [...map.values()];
  for (const row of rows) row.change = change(row.prev, row.cur);
  return rows.sort((a, b) => b.cur - a.cur || b.prev - a.prev || a.name.localeCompare(b.name));
}

/** The summary card's second paragraph: biggest rise, biggest drop (not bills), most extra visits. */
export function restSentence(r: YoyReport): string {
  const cats = categoryRows(r).filter((c) => !c.bill);
  let up: CategoryRow | null = null;
  let down: CategoryRow | null = null;
  for (const c of cats) {
    const d = c.cur - c.prev;
    if (d >= 1 && (!up || d > up.cur - up.prev)) up = c;
    if (d <= -1 && (!down || d < down.cur - down.prev)) down = c;
  }
  let more: StoreRow | null = null;
  for (const s of storeRows(r)) {
    const d = s.curCount - s.prevCount;
    if (d > 0 && (!more || d > more.curCount - more.prevCount)) more = s;
  }
  const parts: string[] = [];
  if (up) parts.push(`${up.name} went up the most, ${money(up.cur - up.prev)} more.`);
  if (down) parts.push(`${down.name} went down the most, ${money(down.cur - down.prev)} less.`);
  if (more) {
    const n = more.curCount - more.prevCount;
    parts.push(`You went to ${more.name} ${n === 1 ? '1 more time' : `${n.toLocaleString('en-US')} more times`} than last year.`);
  }
  return parts.join(' ');
}

export interface Stat {
  label: string;
  value: string;
  sub: string;
  /** cur = this year's tint, prev = neutral, up = amber, down = green, same = neutral. */
  tone: 'cur' | 'prev' | Trend;
}

/** The three stats: this year, last year, the difference. */
export function yoyStats(r: YoyReport): Stat[] {
  const curYear = yearOf(r.current);
  const prevYear = yearOf(r.previous);
  const d = r.current.total - r.previous.total;
  const c = change(r.previous.total, r.current.total);
  const pct = r.previous.total > 0.004 ? Math.round((Math.abs(d) / r.previous.total) * 1000) / 10 : null;
  // A finished month: "All of March 2026" / "All of March 2025", "compared with March 2025".
  const whole = isWholeMonth(r);
  const span = (side: YoySide, year: string) => (whole ? `All of ${monthLong(side.start)} ${year}` : `${spanText(side)}, ${year}`);
  const against = whole ? `${monthLong(r.previous.start)} ${prevYear}` : prevYear;
  return [
    { label: curYear, value: money(r.current.total), sub: span(r.current, curYear), tone: 'cur' },
    { label: prevYear, value: money(r.previous.total), sub: span(r.previous, prevYear), tone: 'prev' },
    {
      label: 'Difference',
      value: c.trend === 'same' ? 'About the same' : `${money(d)} ${d > 0 ? 'more' : 'less'}`,
      sub: pct === null ? `Compared with ${against}` : `${d >= 0 ? '+' : '−'}${pct}% compared with ${against}`,
      tone: c.trend,
    },
  ];
}

// ---------------------------------------------------------------- month by month

export interface MonthBar {
  month: string;
  label: string;
  prev: number;
  cur: number;
  /** "+$212" / "−$185" / "Same" */
  diff: string;
  trend: Trend;
}

/** Paired months, matched by position (each side has its own year). */
export function monthBars(r: YoyReport): MonthBar[] {
  return r.current.months.map((m, i) => {
    const prev = r.previous.months[i]?.total ?? 0;
    const d = m.total - prev;
    const trend: Trend = Math.abs(d) < 1 ? 'same' : d > 0 ? 'up' : 'down';
    return { month: m.month, label: monthAbbr(m.month), prev, cur: m.total, diff: trend === 'same' ? 'Same' : `${d > 0 ? '+' : '−'}${money(d)}`, trend };
  });
}

/** "Everything you spent, bills included. September shows the 1st to the 22nd in both years." */
export function chartSubtitle(r: YoyReport): string {
  const base = 'Everything you spent, bills included.';
  const end = r.current.end;
  const [y, m] = end.split('-').map(Number) as [number, number];
  const last = new Date(y, m, 0).getDate();
  const day = dayOf(end);
  if (day >= last) return base;
  const month = monthLong(end);
  return day === 1 ? `${base} ${month} shows only the 1st in both years.` : `${base} ${month} shows the 1st to the ${ordinal(day)} in both years.`;
}

/** The chart's spoken summary. */
export function chartLabel(r: YoyReport): string {
  const py = yearOf(r.previous);
  const cy = yearOf(r.current);
  return `Spending by month, ${py} compared with ${cy}: ${monthBars(r)
    .map((b) => `${monthLong(b.month)} ${money(b.prev)} and ${money(b.cur)}`)
    .join('; ')}.`;
}

// ---------------------------------------------------------------- stores and places

/** Client-side search: the name contains the words typed (any case). */
export function filterStores(rows: StoreRow[], query: string): StoreRow[] {
  const q = query.trim().toLowerCase();
  return q ? rows.filter((s) => s.name.toLowerCase().includes(q)) : rows;
}

export const noMatchText = (query: string) => `No store matches “${query.trim()}”. Try another name.`;

/** "$2,140 · 62 visits" */
export const storeSide = (total: number, count: number) => `${money(total)} · ${visits(count)}`;

/** The expanded line: "14 fewer visits than last year. Average visit went from $34.52 to $37.71." */
export function storeDetail(s: StoreRow): string {
  if (s.prevCount === 0) return s.curCount ? `New this year: ${visits(s.curCount)}, ${money2(s.cur / s.curCount)} on average.` : 'Nothing spent here in either year.';
  if (s.curCount === 0) return `No visits this year. Last year: ${visits(s.prevCount)}, ${money2(s.prev / s.prevCount)} on average.`;
  const d = s.curCount - s.prevCount;
  const first = d === 0 ? 'The same number of visits as last year.' : `${visits(Math.abs(d)).replace(' visit', d > 0 ? ' more visit' : ' fewer visit')} than last year.`;
  return `${first} Average visit went from ${money2(s.prev / s.prevCount)} to ${money2(s.cur / s.curCount)}.`;
}
