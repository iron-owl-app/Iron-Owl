import { useId, useMemo, useState, type CSSProperties } from 'react';
import { api, type YoyMode, type YoyReport } from '../../api';
import { useApi } from '../../lib/useApi';
import { todayInfo } from '../../lib/reports';
import { EmptyState } from '../../components/EmptyState';
import { ErrorPanel } from '../../components/ErrorPanel';
import { MonthPicker } from '../../components/MonthPicker';
import { PillPicker } from '../../components/PillPicker';
import { Skeleton } from '../../components/ui';
import { monthsBetween } from '../../lib/monthPickerMath';
import {
  categoryRows,
  chartLabel,
  chartSubtitle,
  compareNote,
  emptyText,
  filterStores,
  isEmpty,
  leadSentence,
  modeLabel,
  money,
  monthBars,
  noMatchText,
  restSentence,
  storeDetail,
  storeRows,
  storeSide,
  yearOf,
  yoyStats,
  type Change,
  type Stat,
} from './yoyMath';
import './yoy.css';

/**
 * Reports › Year over year (Release 3.14, reports-v2 design Screen 3): the same days this year and
 * last year, by month, by category and by store, from `GET /api/reports/yoy?mode=year|month`.
 * One month (Release 3.18) can be any month back to the first data (`&month=YYYY-MM`): this month
 * is "so far", a finished month is the whole month vs the whole month last year. The words come
 * from `yoyMath.ts`.
 */

/** Stores shown before "Show all": the rest are a click (or a search) away. */
const STORE_LIMIT = 12;

const STAT_HUE: Record<Stat['tone'], number | null> = {
  cur: 200,
  prev: null,
  up: 80,
  down: 155,
  same: null,
};

export function YoyTab() {
  const today = useMemo(() => todayInfo(), []);
  const [mode, setMode] = useState<YoyMode>('year');
  const [month, setMonth] = useState(today.month);
  const r = useApi(() => api.reports.yoy(mode, mode === 'month' ? month : undefined), [mode, month]);
  const data = r.data && r.data.mode === mode && (mode === 'year' || r.data.month === month) ? r.data : null;
  // The picker's months: the first data month (from any answer so far) to this month.
  const first = r.data?.first_month ?? null;
  const months = useMemo(() => {
    const list = first ? monthsBetween(first, today.month) : [];
    return list.length ? list : [today.month];
  }, [first, today.month]);

  let body;
  if (!data && r.error) body = <ErrorPanel error={r.error} onRetry={r.reload} title="Couldn’t load Year over year" />;
  else if (!data) body = <YoySkeleton />;
  // "Not enough history yet" only for This year so far; a picked month always shows the page
  // (rows say "new this year" when last year had nothing).
  else if (mode === 'year' && isEmpty(data))
    body = (
      <div className="ryo-card">
        <EmptyState kind="chart" title="Not enough history yet" compact>
          {emptyText(data.first_month)}
        </EmptyState>
      </div>
    );
  else body = <YoyBody key={`${mode}-${month}`} data={data} thisYear={today.month.slice(0, 4)} />;

  // No data at all: only the EmptyState, no toggle above it. With some data (under a year),
  // the toggle stays so One month is still reachable.
  const empty = mode === 'year' && !!data && isEmpty(data) && !data.first_month;

  return (
    <div className="ryo-tab">
      {!empty && (
        <div className="ryo-controls">
          <PillPicker<YoyMode>
            label="Compare"
            value={mode}
            onChange={setMode}
            options={[
              { id: 'year', label: modeLabel('year') },
              { id: 'month', label: modeLabel('month') },
            ]}
          />
          {mode === 'month' && <MonthPicker months={months} value={month} onChange={setMonth} thisMonth={today.month} label="Month to compare" />}
          <span className="ryo-note">{compareNote(today.month, mode === 'month' ? month : null)}</span>
        </div>
      )}
      {body}
    </div>
  );
}

function YoySkeleton() {
  return (
    <div className="ryo-body" role="status" aria-label="Loading Year over year">
      <Skeleton height={120} style={{ borderRadius: 16 }} />
      <div className="ryo-stats">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} height={104} style={{ borderRadius: 16 }} />
        ))}
      </div>
      <Skeleton height={220} style={{ borderRadius: 16 }} />
    </div>
  );
}

function YoyBody({ data, thisYear }: { data: YoyReport; thisYear: string }) {
  const stats = yoyStats(data);
  const rest = restSentence(data);
  return (
    <div className="ryo-body">
      <section className="ryo-summary" aria-label="Year over year summary">
        <p className="ryo-lead">{leadSentence(data, thisYear)}</p>
        {rest && <p className="ryo-rest">{rest}</p>}
      </section>
      <dl className="ryo-stats">
        {stats.map((s) => {
          const hue = STAT_HUE[s.tone];
          return (
            <div
              key={s.label}
              className={`ryo-stat${hue === null ? '' : ' is-tinted'}`}
              style={hue === null ? undefined : ({ '--h': hue } as CSSProperties)}
            >
              <dt>{s.label}</dt>
              <dd className="ryo-stat-v num">{s.value}</dd>
              <dd className="ryo-stat-s">{s.sub}</dd>
            </div>
          );
        })}
      </dl>
      {data.mode === 'year' && <MonthChart data={data} />}
      <div className="ryo-two">
        <Categories data={data} />
        <Stores data={data} />
      </div>
    </div>
  );
}

function Legend({ data }: { data: YoyReport }) {
  return (
    <span className="ryo-legend" aria-hidden="true">
      <span>
        <i className="is-prev" />
        {yearOf(data.previous)}
      </span>
      <span>
        <i className="is-cur" />
        {yearOf(data.current)}
      </span>
    </span>
  );
}

function MonthChart({ data }: { data: YoyReport }) {
  const headId = useId();
  const bars = monthBars(data);
  const max = Math.max(0, ...bars.flatMap((b) => [b.prev, b.cur]));
  const h = (v: number) => `${max > 0 ? (Math.max(0, v) / max) * 100 : 0}%`;
  return (
    <section className="ryo-card" aria-labelledby={headId}>
      <div className="ryo-card-head">
        <h2 id={headId} className="ryo-h2">
          Month by month
        </h2>
        <Legend data={data} />
      </div>
      <p className="ryo-sub">{chartSubtitle(data)}</p>
      {/* Phones: the chart keeps readable columns and scrolls sideways inside its card. */}
      <div className="ryo-chart-scroll" tabIndex={0} role="group" aria-label="Month by month chart">
        <div
          className="ryo-chart"
          role="img"
          aria-label={chartLabel(data)}
          style={{
            gridTemplateColumns: `repeat(${bars.length}, minmax(3.5rem, 1fr))`,
          }}
        >
          {bars.map((b) => (
            <div key={b.month} className="ryo-chart-col">
              <div className="ryo-chart-pair">
                <span className="ryo-bar is-prev" style={{ height: h(b.prev) }} />
                <span className="ryo-bar is-cur" style={{ height: h(b.cur) }} />
              </div>
              <span className="ryo-chart-m">{b.label}</span>
              <span className={`ryo-chart-d is-${b.trend}`}>{b.diff}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Tag({ c }: { c: Change }) {
  return <span className={`ryo-tag is-${c.trend}`}>{c.tag}</span>;
}

function Categories({ data }: { data: YoyReport }) {
  const headId = useId();
  const rows = categoryRows(data);
  const max = Math.max(0, ...rows.flatMap((r) => [r.prev, r.cur]));
  const w = (v: number) => `${max > 0 ? (Math.max(0, v) / max) * 100 : 0}%`;
  const py = yearOf(data.previous);
  const cy = yearOf(data.current);
  return (
    <section className="ryo-card" aria-labelledby={headId}>
      <h2 id={headId} className="ryo-h2">
        Categories
      </h2>
      <p className="ryo-sub">
        Top bar is {py}, bottom bar is {cy}.
      </p>
      <ul className="ryo-cats">
        {rows.map((r) => (
          <li key={r.id} style={{ '--h': r.hue } as CSSProperties}>
            <span className="ryo-cat-line">
              <span className="ryo-cat-name">
                <span className="ryo-dot" aria-hidden="true" />
                {r.name}
              </span>
              <Tag c={r.change} />
              <span className="ryo-words">{r.change.words}</span>
            </span>
            <span className="ryo-cat-bars">
              <span className="ryo-muted">{py}</span>
              <span className="ryo-track" aria-hidden="true">
                <span className="is-prev" style={{ width: w(r.prev) }} />
              </span>
              <span className="ryo-amt ryo-muted num">{money(r.prev)}</span>
              <span className="ryo-strong">{cy}</span>
              <span className="ryo-track" aria-hidden="true">
                <span className="is-cat" style={{ width: w(r.cur) }} />
              </span>
              <span className="ryo-amt ryo-strong num">{money(r.cur)}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Stores({ data }: { data: YoyReport }) {
  const headId = useId();
  const inputId = useId();
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const rows = useMemo(() => storeRows(data), [data]);
  const found = filterStores(rows, query);
  const searching = query.trim() !== '';
  const shown = searching || all ? found : found.slice(0, STORE_LIMIT);
  const py = yearOf(data.previous);
  const cy = yearOf(data.current);

  return (
    <section className="ryo-card" aria-labelledby={headId}>
      <h2 id={headId} className="ryo-h2">
        Stores and places
      </h2>
      <p className="ryo-sub">How much you spent at each place, and how often you went. Click one for details.</p>
      {rows.length === 0 ? (
        <p className="ryo-none">No stores to compare yet.</p>
      ) : (
        <>
          <label className="ryo-search" htmlFor={inputId}>
            <span>Find a store</span>
            <input
              id={inputId}
              type="search"
              value={query}
              placeholder="e.g. Walmart"
              autoComplete="off"
              spellCheck={false}
              maxLength={80}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
          {searching && !found.length && (
            <p className="ryo-none" role="status">
              {noMatchText(query)}
            </p>
          )}
          <ul className="ryo-stores">
            {shown.map((s) => {
              const isOpen = open === s.name;
              return (
                <li key={s.name} className={isOpen ? 'is-open' : undefined}>
                  <button type="button" className="ryo-store" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : s.name)}>
                    <span className="ryo-store-line">
                      <span className="ryo-store-name">
                        <strong>{s.name}</strong>
                        {s.category && <span className="ryo-muted"> · {s.category}</span>}
                      </span>
                      <Tag c={s.change} />
                      <span className="ryo-chev" aria-hidden="true">
                        {isOpen ? '▾' : '▸'}
                      </span>
                    </span>
                    <span className="ryo-store-nums">
                      <span className="ryo-muted">
                        {py}: {storeSide(s.prev, s.prevCount)}
                      </span>
                      <span className="ryo-strong">
                        {cy}: {storeSide(s.cur, s.curCount)}
                      </span>
                      <span>{s.change.words}</span>
                    </span>
                  </button>
                  {isOpen && <p className="ryo-store-detail">{storeDetail(s)}</p>}
                </li>
              );
            })}
          </ul>
          {!searching && !all && found.length > STORE_LIMIT && (
            <button type="button" className="rp-btn ryo-more" onClick={() => setAll(true)}>
              Show all {found.length} stores
            </button>
          )}
        </>
      )}
    </section>
  );
}
