import { useId, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import { api, type MonthlyReport } from '../../api';
import { useApi } from '../../lib/useApi';
import { monthName } from '../../lib/budget';
import { REPORT_MONTHS, firstActiveIndex, short, todayInfo, usualOf, whole, type Today } from '../../lib/reports';
import { EmptyState } from '../../components/EmptyState';
import { ErrorPanel } from '../../components/ErrorPanel';
import { Skeleton } from '../../components/ui';
import { useReportActions } from './useReportActions';
import {
  WEEKDAYS,
  WEEKDAY_SHORT,
  calendarSubtitle,
  capIdea,
  cellAmount,
  cellAmountShort,
  cellLevel,
  dayStatus,
  isOpenToday,
  latestNoSpendDay,
  money,
  money2,
  monthLong,
  noSpendIdea,
  perMonth,
  prepareHabits,
  priceNote,
  smallMeta,
  smallYearly,
  subscriptionsLead,
  subscriptionsTotal,
  visitsIdea,
  weekdayAverages,
  weekdayExtremes,
  weekdayLead,
  type CapInput,
  type HabitDay,
  type HabitsData,
  type Idea,
} from './habitsMath';
import './habits.css';

/**
 * Reports › Habits (Release 3.14, reports-v2 design Screen 2): Habits worth trying (3 idea cards,
 * one button each), the spending calendar, by day of the week, small purchases, and
 * subscriptions. The current month only; day-to-day spending, bills left out. The words come
 * from `habitsMath.ts`.
 */

/** Idea card hues: cap = amber, no-spend = green, visits = coral. */
const IDEA_HUE: Record<Idea['kind'], number> = {
  cap: 80,
  nospend: 155,
  visits: 25,
};

const reducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

export function HabitsTab() {
  const today = useMemo(() => todayInfo(), []);
  const report = useApi(() => api.reports.monthly(REPORT_MONTHS), []);
  const habits = useApi(() => api.reports.habits(today.month), [today.month]);
  const data = useMemo(() => (habits.data ? prepareHabits(habits.data, today) : null), [habits.data, today]);
  const acts = useReportActions({ watchAlert: true });

  const [day, setDay] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const calRef = useRef<HTMLElement>(null);

  const showNoSpend = () => {
    const d = data ? latestNoSpendDay(data) : null;
    setDay(d);
    setNote(d === null ? 'No no-spend days yet this month. A day with no spending turns green here.' : null);
    const behavior: ScrollBehavior = reducedMotion() ? 'auto' : 'smooth';
    calRef.current?.scrollIntoView({ behavior, block: 'start' });
    const btn = d === null ? null : calRef.current?.querySelector<HTMLButtonElement>(`[data-day="${d}"]`);
    btn?.focus({ preventScroll: true });
  };

  const noHistory =
    !!data && !!report.data && data.total <= 0.004 && report.data.months.every((m) => m.spending < 0.005 && m.fixed < 0.005);

  let middle;
  if (!data && habits.error) {
    middle = <ErrorPanel error={habits.error} onRetry={habits.reload} title="Couldn’t load your spending habits" />;
  } else if (!data) {
    middle = <SplitSkeleton />;
  } else if (!habits.data?.through) {
    middle = (
      <div className="rph-card">
        <EmptyState kind="chart" title={`${monthName(today.month)} hasn’t started yet`} compact>
          Your spending calendar and habits fill in from the 1st.
        </EmptyState>
      </div>
    );
  } else if (noHistory) {
    middle = (
      <div className="rph-card">
        <EmptyState kind="chart" title="No spending to look at yet" compact>
          Once transactions come in, this shows your spending by day and by weekday, and how small purchases add up.
        </EmptyState>
      </div>
    );
  } else {
    middle = (
      <div className="rph-split">
        <CalendarCard
          data={data}
          day={day}
          note={note}
          sectionRef={calRef}
          onPick={(d) => {
            setNote(null);
            setDay((cur) => (cur === d ? null : d));
          }}
        />
        <div className="rph-col">
          <WeekdayCard data={data} />
          <SmallCard data={data} />
        </div>
      </div>
    );
  }

  return (
    <div className="rph-tab">
      <Ideas
        report={report.data}
        data={data}
        today={today}
        loading={(!report.data && report.loading) || (!data && habits.loading)}
        acts={acts}
        onNoSpend={showNoSpend}
      />
      {middle}
      <SubscriptionsCard month={today.month} />
      {acts.toast}
    </div>
  );
}

function SplitSkeleton() {
  return (
    <div className="rph-split" role="status" aria-label="Loading your habits">
      <div className="rph-card">
        <Skeleton width="45%" height={18} />
        <Skeleton className="skel-text" width="75%" />
        <Skeleton height={300} style={{ marginTop: 12, borderRadius: 10 }} />
      </div>
      <div className="rph-col">
        {[0, 1].map((i) => (
          <div className="rph-card" key={i}>
            <Skeleton width="45%" height={18} />
            <Skeleton height={120} style={{ marginTop: 12, borderRadius: 10 }} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Habits worth trying

function capInputs(report: MonthlyReport, today: Today): CapInput[] {
  const last = report.months.length - 1;
  const m = report.months[last];
  if (!m || m.month !== today.month) return [];
  return report.categories
    .filter((c) => c.kind === 'spending' && !c.bill)
    .map((c) => ({
      id: c.id,
      name: c.name,
      spent: m.by_category[c.id] ?? 0,
      plan: m.planned?.[c.id] ?? 0,
      usual: usualOf(report, c.id),
    }));
}

function Ideas({
  report,
  data,
  today,
  loading,
  acts,
  onNoSpend,
}: {
  report: MonthlyReport | undefined;
  data: HabitsData | null;
  today: Today;
  loading: boolean;
  acts: ReturnType<typeof useReportActions>;
  onNoSpend: () => void;
}) {
  const headId = useId();
  const ideas = useMemo(() => {
    const out: (Idea | null)[] = [report ? capIdea(capInputs(report, today), today) : null];
    if (data) out.push(noSpendIdea(data), visitsIdea(data));
    return out.filter((x): x is Idea => x !== null);
  }, [report, data, today]);
  const month = monthName(today.month);
  const hasHistory = !!report && report.months.length - firstActiveIndex(report) > 1;

  return (
    <section className="rph-ideas" aria-labelledby={headId}>
      <div>
        <h2 id={headId} className="rph-h2">
          Habits worth trying
        </h2>
        <p className="rph-sub">Based on {month} so far.</p>
      </div>
      {loading && !ideas.length ? (
        <div className="rph-idea-grid" role="status" aria-label="Loading ideas">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} height={180} style={{ borderRadius: 16 }} />
          ))}
        </div>
      ) : !ideas.length ? (
        <p className="rph-none">
          {hasHistory ? 'Nothing stands out to work on right now. Nice going.' : 'Ideas show up once a week or so of spending is in.'}
        </p>
      ) : (
        <div className="rph-idea-grid">
          {ideas.map((idea, i) => (
            <IdeaCard key={idea.kind} n={i + 1} idea={idea}>
              {idea.kind === 'cap' && idea.cap ? (
                <button
                  type="button"
                  className="rph-idea-btn"
                  disabled={acts.busy !== null}
                  aria-busy={acts.busy === `plan:${idea.cap.id}` || undefined}
                  onClick={() => void acts.planNextMonth(today.month, { id: idea.cap!.id, name: idea.cap!.name }, idea.cap!.amount)}
                >
                  {acts.busy === `plan:${idea.cap.id}` ? 'Saving…' : 'Set the cap'}
                </button>
              ) : idea.kind === 'nospend' ? (
                <button type="button" className="rph-idea-btn" onClick={onNoSpend}>
                  See my no-spend days
                </button>
              ) : idea.kind === 'visits' ? (
                acts.alertOn === true ? (
                  <button type="button" className="rph-idea-btn" disabled>
                    <span aria-hidden="true">✓</span> Reminder is on
                  </button>
                ) : (
                  <button
                    type="button"
                    className="rph-idea-btn"
                    disabled={acts.busy !== null}
                    aria-busy={acts.busy === 'alert' || undefined}
                    onClick={() => void acts.turnOnBudgetAlert()}
                  >
                    {acts.busy === 'alert' ? 'Turning on…' : 'Remind me'}
                  </button>
                )
              ) : null}
            </IdeaCard>
          ))}
        </div>
      )}
    </section>
  );
}

function IdeaCard({ n, idea, children }: { n: number; idea: Idea; children: ReactNode }) {
  const titleId = useId();
  return (
    <article className="rph-idea" style={{ '--h': IDEA_HUE[idea.kind] } as CSSProperties} aria-labelledby={titleId}>
      <span className="rph-idea-n" aria-hidden="true">
        {n}
      </span>
      <h3 id={titleId} className="rph-idea-title">
        {idea.title}
      </h3>
      <p className="rph-idea-body">{idea.body}</p>
      {children}
    </article>
  );
}

// ---------------------------------------------------------------- Spending calendar

function CalendarCard({
  data,
  day,
  note,
  sectionRef,
  onPick,
}: {
  data: HabitsData;
  day: number | null;
  note: string | null;
  sectionRef: RefObject<HTMLElement>;
  onPick: (day: number) => void;
}) {
  const headId = useId();
  const month = monthLong(data.month);
  const last = data.days.length;
  const sel = day !== null && day <= last ? day : null;

  const label = (d: HabitDay) => {
    const when = `${WEEKDAYS[d.dow]}, ${month} ${d.day}`;
    if (d.spent > 0.004) return `${when}: ${money(d.spent)}`;
    return isOpenToday(data, d) ? `${when}: today, nothing spent so far` : `${when}: no spending`;
  };

  return (
    <section className="rph-card rph-cal-card" aria-labelledby={headId} ref={sectionRef}>
      <h2 id={headId} className="rph-h2">
        Spending calendar
      </h2>
      <p className="rph-sub">{calendarSubtitle(data)}</p>
      <div className="rph-cal rph-cal-dow" aria-hidden="true">
        {WEEKDAY_SHORT.map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>
      <div className="rph-cal" role="group" aria-label={`Spending calendar for ${month}`}>
        {Array.from({ length: data.lead }, (_, i) => (
          <span key={`b${i}`} className="rph-day is-blank" aria-hidden="true" />
        ))}
        {data.days.map((d) => {
          const open = isOpenToday(data, d);
          const kind = d.spent > 0.004 ? 'is-spent' : open ? 'is-today' : 'is-nospend';
          const lv = cellLevel(data, d);
          const compact = cellAmountShort(data, d);
          return (
            <button
              key={d.day}
              type="button"
              data-day={d.day}
              className={`rph-day ${kind}${lv >= 0.6 ? ' is-strong' : ''}`}
              style={{ '--lv': lv.toFixed(3) } as CSSProperties}
              aria-label={label(d)}
              aria-pressed={d.day === sel}
              onClick={() => onPick(d.day)}
            >
              <span className="rph-day-n">{d.day}</span>
              <span className="rph-day-amt is-full">{cellAmount(data, d)}</span>
              {/* Phones: a compact amount that never wraps, or a dot for a no-spend day (the label says it). */}
              <span className="rph-day-amt is-short" aria-hidden="true">
                {compact || <span className="rph-day-dot" />}
              </span>
            </button>
          );
        })}
        {Array.from({ length: data.daysInMonth - last }, (_, i) => (
          <button key={`f${i}`} type="button" className="rph-day is-future" disabled aria-label={`${month} ${last + i + 1}, still to come`}>
            <span className="rph-day-n">{last + i + 1}</span>
          </button>
        ))}
      </div>
      <div className="rph-cal-legend" aria-hidden="true">
        <span>
          <i className="rph-sw is-nospend" />
          No-spend day
        </span>
        <span>
          Less
          <i className="rph-sw" style={{ '--lv': 0.15 } as CSSProperties} />
          <i className="rph-sw" style={{ '--lv': 0.55 } as CSSProperties} />
          <i className="rph-sw" style={{ '--lv': 1 } as CSSProperties} />
          More
        </span>
      </div>
      <p className="rph-status" role="status">
        {note ?? dayStatus(data, sel)}
      </p>
    </section>
  );
}

// ---------------------------------------------------------------- By day of the week

function WeekdayCard({ data }: { data: HabitsData }) {
  const headId = useId();
  const avgs = weekdayAverages(data);
  const ready = data.complete.length >= 7 && data.total > 0.004;
  const ex = ready ? weekdayExtremes(avgs) : null;
  const max = Math.max(0, ...avgs.map((v) => v ?? 0));
  const spoken = `Average day-to-day spending by weekday: ${avgs.map((v, i) => `${WEEKDAYS[i]} ${v === null ? 'no days yet' : whole(v)}`).join(', ')}.`;
  return (
    <section className="rph-card" aria-labelledby={headId}>
      <h2 id={headId} className="rph-h2">
        By day of the week
      </h2>
      <p className="rph-sub">{weekdayLead(data)}</p>
      <div className="rph-wk" role="img" aria-label={spoken}>
        {avgs.map((v, i) => {
          const tone = ex?.max === i ? ' is-max' : ex?.min === i ? ' is-min' : '';
          return (
            <div key={i} className={`rph-wk-col${tone}`}>
              <span className="rph-wk-val num">{v === null ? '—' : short(v)}</span>
              <span className="rph-wk-track">
                <span className="rph-wk-bar" style={{ height: `${max > 0 && v ? (v / max) * 100 : 0}%` }} />
              </span>
              <span className="rph-wk-day">{WEEKDAY_SHORT[i]}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- Small purchases add up

function SmallCard({ data }: { data: HabitsData }) {
  const headId = useId();
  const { small } = data;
  const month = monthLong(data.month);
  const under = money(small.under);
  const yearly = smallYearly(data);
  const top = small.merchants.slice(0, 4);
  const topMax = Math.max(0, ...top.map((m) => m.total));
  return (
    <section className="rph-card" aria-labelledby={headId}>
      <h2 id={headId} className="rph-h2">
        Small purchases add up
      </h2>
      <p className="rph-sub">
        Purchases under {under} in {month} so far.
      </p>
      {small.count === 0 ? (
        <p className="rph-none">No purchases under {under} yet. Nothing to trim here.</p>
      ) : (
        <>
          <div className="rph-small-total">
            <strong className="rph-big num">{money2(small.total)}</strong>
            <span className="rph-muted">in {small.count === 1 ? '1 purchase' : `${small.count.toLocaleString()} purchases`}</span>
            {yearly && <span className="rph-tag is-up">{yearly}</span>}
          </div>
          <ul className="rph-merch">
            {top.map((m) => (
              <li key={m.name}>
                <span className="rph-merch-line">
                  <span className="rph-merch-name">
                    {m.name} <span className="rph-muted">· {smallMeta(m)}</span>
                  </span>
                  <strong className="num">{money2(m.total)}</strong>
                </span>
                <span className="rph-track" aria-hidden="true">
                  <span
                    style={{
                      width: `${topMax > 0 ? (m.total / topMax) * 100 : 0}%`,
                    }}
                  />
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- Subscriptions (recurring charges on a credit card)

function SubscriptionsCard({ month }: { month: string }) {
  const subs = useApi(() => api.reports.subscriptions(), []);
  const headId = useId();
  if (!subs.data && subs.error) return <ErrorPanel error={subs.error} onRetry={subs.reload} title="Couldn’t load your subscriptions" />;
  const items = subs.data?.items ?? [];
  return (
    <section className="rph-card rph-subs" aria-labelledby={headId}>
      <h2 id={headId} className="rph-h2">
        Subscriptions
      </h2>
      {!subs.data ? (
        <div role="status" aria-label="Loading subscriptions">
          <Skeleton className="skel-text" width="60%" />
          <Skeleton height={140} style={{ marginTop: 10, borderRadius: 10 }} />
        </div>
      ) : !items.length ? (
        <p className="rph-sub">
          No subscriptions found. Add them on the{' '}
          <Link to="/recurring" className="rph-inline-link">
            Bills and paychecks page
          </Link>
          .
        </p>
      ) : (
        <>
          <p className="rph-sub">{subscriptionsLead(items)}</p>
          <ul className="rph-subs-list">
            {items.map((s) => {
              const note = priceNote(s, month);
              return (
                <li key={s.id}>
                  <span className="rph-subs-name">
                    <strong>{s.name}</strong>
                    {note &&
                      (note.tone === 'same' ? (
                        <span className="rph-muted rph-small-note">{note.text}</span>
                      ) : (
                        <span className={`rph-tag ${note.tone === 'up' ? 'is-up' : 'is-down'}`}>{note.text}</span>
                      ))}
                  </span>
                  <strong className="num">{perMonth(s.monthly)}</strong>
                </li>
              );
            })}
          </ul>
          <div className="rph-subs-foot">
            <strong className="num">{subscriptionsTotal(subs.data.monthly_total)}</strong>
            <Link to="/recurring" className="rp-link">
              See them on the calendar <span aria-hidden="true">→</span>
            </Link>
          </div>
        </>
      )}
    </section>
  );
}
