import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { api, type MonthlyReport, type MonthlyReportCategory } from '../../api';
import { useApi } from '../../lib/useApi';
import { readPref, writePref } from '../../lib/prefs';
import { groupHue } from '../../lib/reports';
import {
  DUST,
  FETCH_MONTHS,
  changesOf,
  clampMonth,
  figuresOf,
  isPeriod,
  moneyOf,
  monthsToFetch,
  pickerMonths,
  statCards,
  sum,
  summaryText,
  todayInfo,
  viewOf,
  type CatFig,
  type Period,
  type Today,
} from '../../lib/reportsMath';
import { Skeleton } from '../../components/ui';
import { EmptyState } from '../../components/EmptyState';
import { ErrorPanel } from '../../components/ErrorPanel';
import { PillPicker } from '../../components/PillPicker';
import { MonthPicker } from '../../components/MonthPicker';
import { MoneyInOut } from './overview/MoneyInOut';
import { PlanVsSpent } from './overview/PlanVsSpent';
import { HigherLower } from './overview/HigherLower';
import { CategoryPanel } from './overview/CategoryPanel';
import { useReportActions } from './useReportActions';
import './overview.css';

/**
 * Reports › Overview (Release 3.14, reports-v2 design layout "A · Summary first"): controls
 * (period, month picker back to the first month with data, Hide fixed bills), the summary sentence, three stat cards, Money in
 * and out, Plan vs. what you spent | Running higher or lower, and the category panel beside
 * them (sticky on wide screens, above the content on narrow ones).
 */

const PERIODS: { id: Period; label: string }[] = [
  { id: 'month', label: 'One month' },
  { id: 'q', label: 'Last 3 months' },
  { id: 'h', label: 'Last 6 months' },
];

export function OverviewTab() {
  const [period, setPeriodState] = useState<Period>(() => readPref('reports.period', 'month', isPeriod));
  // SPEC 3.14: on for new users; a stored value is kept.
  const [hideBills, setHideBillsState] = useState<boolean>(() => readPref('reports.hideBills', true));
  // One month: the month picked (null = the newest, this month) and how many months to load.
  // The count only grows, so going back to a newer month doesn't load again.
  const [picked, setPicked] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(FETCH_MONTHS);
  const today = useMemo(() => todayInfo(), []);
  const report = useApi(() => api.reports.monthly(fetchCount), [fetchCount]);
  const data = report.data;

  const setPeriod = (p: Period) => {
    setPeriodState(p);
    writePref('reports.period', p);
  };
  const toggleBills = () => {
    setHideBillsState(!hideBills);
    writePref('reports.hideBills', !hideBills);
  };

  const empty = !!data && (!data.months.length || data.months.every((m) => m.income < 0.005 && m.spending < 0.005 && m.fixed < 0.005));
  const months = data && data.months.length ? pickerMonths(data) : null;
  const newest = months ? months[months.length - 1]! : null;
  const shown = months && newest ? clampMonth(picked ?? newest, months) : null;
  const at = data && shown ? data.months.findIndex((m) => m.month === shown) : -1;
  // Months needed for the shown month and its usual months; an older month loads more.
  const need = shown && newest ? monthsToFetch(shown, newest) : FETCH_MONTHS;
  useEffect(() => {
    if (need > fetchCount) setFetchCount(need);
  }, [need, fetchCount]);
  // Still loading them: the body waits (skeleton) instead of showing another month's numbers.
  // A finished load that still came back short (a lower server limit) stops waiting.
  const waiting = period === 'month' && !!data && data.months.length < need && (report.loading || fetchCount < need);

  return (
    <div className="rpo">
      <div className="rpo-controls">
        <div className="rpo-period">
          <PillPicker label="Period" value={period} onChange={setPeriod} options={PERIODS} />
          {period === 'month' && months && shown && !empty && (
            <MonthPicker months={months} value={shown} onChange={setPicked} thisMonth={today.month} label="Month to show" />
          )}
        </div>
        <div className="rpo-hide">
          <button type="button" role="switch" aria-checked={hideBills} aria-describedby="rpo-hide-note" className="rpo-switch" onClick={toggleBills}>
            <span className="rpo-switch-track" aria-hidden="true" />
            Hide fixed bills
          </button>
          <span id="rpo-hide-note" className="rpo-hide-note">
            Rent, phone and insurance cost the same each month. Hiding them shows the spending your habits control. Left over always counts them.
          </span>
        </div>
      </div>
      {report.error && (!data || waiting) ? (
        <ErrorPanel error={report.error} onRetry={report.reload} />
      ) : !data || waiting ? (
        <OverviewSkeleton />
      ) : empty ? (
        <div className="rpo-card">
          <EmptyState kind="chart" title="Nothing to report yet">
            Reports fill in as transactions arrive from your linked bank and card accounts.
          </EmptyState>
        </div>
      ) : (
        <OverviewBody report={data} period={period} at={at < 0 ? data.months.length - 1 : at} hideBills={hideBills} today={today} loading={report.loading} reload={report.reload} />
      )}
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <div className="rpo-body" aria-busy="true">
      <div className="rpo-summary">
        <Skeleton width="70%" height={26} />
        <Skeleton width="90%" height={18} style={{ marginTop: 10 }} />
      </div>
      <dl className="rpo-stats">
        {['Spent', 'Compared with usual', 'Left over'].map((l) => (
          <div className="rpo-stat" key={l} style={{ '--h': 262 } as CSSProperties}>
            <dt>{l}</dt>
            <dd>
              <Skeleton width={120} height={30} />
            </dd>
          </div>
        ))}
      </dl>
      <div className="rpo-card">
        <Skeleton width="100%" height={180} style={{ borderRadius: 10 }} />
      </div>
    </div>
  );
}

const groupKeyOf = (c: MonthlyReportCategory) => (c.group_id === null ? 'none' : String(c.group_id));

function OverviewBody({
  report,
  period,
  at,
  hideBills,
  today,
  loading,
  reload,
}: {
  report: MonthlyReport;
  period: Period;
  at: number;
  hideBills: boolean;
  today: Today;
  loading: boolean;
  reload: () => void;
}) {
  const v = useMemo(() => viewOf(report, period, at, today), [report, period, at, today]);
  const all = useMemo(() => figuresOf(report, v, today), [report, v, today]);
  const vis = useMemo(() => all.filter((f) => !(hideBills && f.cat.bill)), [all, hideBills]);
  const month = report.months[v.at]!.month;
  const hasGroups = report.groups.length > 0;

  // This month's budget (for "Move $X to savings"): only read when the current month is shown.
  const budget = useApi(() => (v.isCur ? api.budgets.get() : Promise.resolve(null)), [v.isCur]);
  const acts = useReportActions({
    onChange: () => {
      reload();
      budget.reload();
    },
  });

  const [group, setGroup] = useState<string | null>(null);
  const [open, setOpenState] = useState<string | null>(null);
  // The button that opened the panel: focus goes back to it when the panel closes.
  const opener = useRef<HTMLElement | null>(null);
  const setOpen = (id: string | null) => {
    const el = document.activeElement;
    if (id !== null && el instanceof HTMLElement && !el.closest('.rpo-panel')) opener.current = el;
    setOpenState(id);
  };
  const closePanel = () => {
    setOpenState(null);
    const el = opener.current;
    opener.current = null;
    if (el && el.isConnected) window.requestAnimationFrame(() => el.focus());
  };
  // A new period, month or Hide setting keeps the selection only if it's still on screen.
  const visible = (f: CatFig) => f.spent > DUST || f.plan > DUST;
  const groupOk = group === null || vis.some((f) => groupKeyOf(f.cat) === group && visible(f));
  const openOk = open === null || vis.some((f) => f.cat.id === open);
  useEffect(() => {
    if (!groupOk) setGroup(null);
    if (!openOk) setOpenState(null);
  }, [groupOk, openOk]);
  const curGroup = groupOk && hasGroups ? group : null;
  const openFig = openOk && open ? (all.find((f) => f.cat.id === open) ?? null) : null;

  useEffect(() => {
    if (!openFig) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePanel();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [openFig]);

  const changes = useMemo(() => changesOf(all), [all]);
  const money = useMemo(() => moneyOf(report, v, today), [report, v, today]);
  const spent = sum(vis.map((f) => f.spent));
  const paced = sum(vis.map((f) => f.paced));
  const plan = sum(vis.map((f) => f.plan));
  const usual = vis.length && vis.every((f) => f.usual !== null) ? sum(vis.map((f) => f.usual!)) : null;
  const input = { v, month, hideBills, spent, paced, usual, top: changes[0] ?? null, money };
  const summary = summaryText(input);
  const stats = statCards({ ...input, plan, today });

  const groupName = (key: string) => (key === 'none' ? 'Not in a group' : (report.groups.find((g) => String(g.id) === key)?.name ?? 'Not in a group'));
  const hueFor = (c: MonthlyReportCategory) => (hasGroups ? groupHue(report, c.group_id) : c.hue);
  const openCat = (id: string) => {
    const c = report.categories.find((x) => x.id === id);
    if (c && hasGroups && !(hideBills && c.bill)) setGroup(groupKeyOf(c));
    setOpen(id);
  };

  return (
    <div className={`rpo-ws${openFig ? ' has-panel' : ''}${loading ? ' is-loading' : ''}`} aria-busy={loading || undefined}>
      <div className="rpo-body">
        <section className="rpo-summary" aria-label="Summary">
          <p className="rpo-summary-lead">{summary.lead}</p>
          {summary.rest && <p className="rpo-summary-rest">{summary.rest}</p>}
        </section>
        <dl className="rpo-stats">
          {stats.map((s) => (
            <div className="rpo-stat" key={s.key} style={{ '--h': s.hue } as CSSProperties}>
              <dt>{s.label}</dt>
              <dd className={`rpo-stat-value num${s.warn ? ' is-warn' : ''}`}>{s.value}</dd>
              <dd className="rpo-stat-sub">{s.sub}</dd>
            </div>
          ))}
        </dl>
        <MoneyInOut report={report} v={v} today={today} />
        <div className="rpo-cols2">
          <PlanVsSpent
            report={report}
            figs={vis}
            v={v}
            group={curGroup}
            groupName={groupName}
            hueFor={hueFor}
            selected={openFig?.cat.id ?? null}
            onGroup={setGroup}
            onCategory={(id) => (open === id ? closePanel() : setOpen(id))}
          />
          <HigherLower
            report={report}
            v={v}
            figs={all}
            changes={changes}
            month={month}
            budget={budget.data ?? null}
            acts={acts}
            onOpen={openCat}
          />
        </div>
      </div>
      {openFig && (
        <CategoryPanel
          key={openFig.cat.id}
          report={report}
          fig={openFig}
          v={v}
          today={today}
          hue={hueFor(openFig.cat)}
          groupLabel={hasGroups ? groupName(groupKeyOf(openFig.cat)) : null}
          onClose={closePanel}
        />
      )}
      {acts.toast}
    </div>
  );
}
