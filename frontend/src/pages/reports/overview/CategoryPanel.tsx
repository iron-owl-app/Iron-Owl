import { useEffect, useId, useRef, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { api, type MonthlyReport } from '../../../api';
import { useApi } from '../../../lib/useApi';
import { parseISODate, plural } from '../../../lib/format';
import { cents, short } from '../../../lib/reports';
import {
  DUST,
  SHOWN_MONTHS,
  categoryNote,
  daysInMonthOf,
  isCurrentIndex,
  monthAbbr,
  monthWord,
  paceOf,
  spentIn,
  usualAt,
  W,
  type CatFig,
  type Today,
  type View,
} from '../../../lib/reportsMath';
import { Icon } from '../../../components/Icon';
import { Skeleton } from '../../../components/ui';

const dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });

/** First and last day the view covers (the current month runs through today). */
function rangeOf(report: MonthlyReport, v: View, today: Today): { start: string; end: string } {
  const first = report.months[v.idx[0]!]!.month;
  const last = report.months[v.idx[v.idx.length - 1]!]!.month;
  const endDay = last === today.month ? today.day : daysInMonthOf(last);
  return { start: `${first}-01`, end: `${last}-${String(endDay).padStart(2, '0')}` };
}

/**
 * The category panel (README 1f): kicker "Everyday · September", the stats (spent, planned, on
 * pace for, usually), a six-month trend with a dashed "usual" line and a dashed pace bar, the
 * note (amber when running high, green when low), Where exactly, Biggest purchases and a link to
 * the transactions. Sticky beside the content on wide screens; Escape closes it (OverviewTab).
 */
export function CategoryPanel({
  report,
  fig,
  v,
  today,
  hue,
  groupLabel,
  onClose,
}: {
  report: MonthlyReport;
  fig: CatFig;
  v: View;
  today: Today;
  hue: number;
  groupLabel: string | null;
  onClose: () => void;
}) {
  const headId = useId();
  const cat = fig.cat;
  const { start, end } = rangeOf(report, v, today);
  const detail = useApi(() => api.reports.category(cat.id, start, end), [cat.id, start, end]);
  const month = report.months[v.at]!.month;
  const note = categoryNote(fig, v, month, detail.data?.merchants[0]?.name);
  // Narrow screens: the panel sits above the content, so bring it into view (after layout) and
  // move focus to its heading. Wide screens keep focus where it is (the panel is beside it).
  const ref = useRef<HTMLElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!window.matchMedia('(max-width: 1099px)').matches) return;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const raf = window.requestAnimationFrame(() => {
      const el = ref.current;
      if (!el) return;
      headRef.current?.focus({ preventScroll: true });
      // Clear the sticky Reports tab bar, which would otherwise cover the panel's header.
      const bar = document.querySelector('.rp-tabbar');
      const barH = bar && getComputedStyle(bar).position === 'sticky' ? bar.getBoundingClientRect().height : 0;
      el.style.scrollMarginTop = `${Math.round(barH) + 16}px`;
      el.scrollIntoView({ block: 'start', behavior: reduce ? 'auto' : 'smooth' });
    });
    return () => window.cancelAnimationFrame(raf);
  }, [cat.id]);

  // Trend: the last six months, the period's months in the category color, the rest dimmed.
  const n = report.months.length;
  const from = Math.max(0, n - SHOWN_MONTHS);
  const cols = report.months.slice(from).map((m, k) => {
    const i = from + k;
    const value = spentIn(report, cat.id, i);
    const pace = paceOf(value, i, report, today, cat.bill);
    return { month: m.month, value, ghost: isCurrentIndex(report, i, today) && pace > value + 0.5 ? pace : 0, on: v.idx.includes(i) };
  });
  const usual = usualAt(report, cat.id, v.period === 'month' ? v.at : n - 1);
  const mx = Math.max(...cols.map((c) => Math.max(c.value, c.ghost)), usual ?? 0) * 1.12 || 1;
  const h = (x: number) => `${((x / mx) * 100).toFixed(1)}%`;
  const trendLabel = `${cat.name} by month: ${cols.map((c) => `${monthAbbr(c.month)} ${W(c.value)}`).join(', ')}.${usual !== null && usual > DUST ? ` Usual ${W(usual)}.` : ''}`;

  const stats: { k: string; v: string }[] = [
    { k: v.isCur ? 'Spent so far' : 'Spent', v: W(fig.spent) },
    { k: 'Planned', v: fig.plan > DUST ? W(fig.plan) : 'No plan' },
  ];
  if (v.isCur) stats.push({ k: 'On pace for', v: W(fig.paced) });
  stats.push({ k: 'Usually', v: fig.usual === null ? 'Not enough history' : W(fig.usual) });

  const merchants = detail.data?.merchants ?? [];
  const topSpent = merchants[0]?.spent || 1;
  const perMonth = v.idx.length > 1 ? ' a month' : '';
  const style = { '--h': hue } as CSSProperties;

  return (
    <aside ref={ref} className="rpo-panel" aria-labelledby={headId} style={style}>
      <header className="rpo-panel-head">
        <div className="rpo-panel-title">
          <p className="rpo-kicker">
            {groupLabel ? `${groupLabel} · ` : ''}
            {v.period === 'month' ? monthWord(month) : v.label}
          </p>
          <h2 id={headId} ref={headRef} tabIndex={-1}>
            <span className="rpo-dot" aria-hidden="true" />
            {cat.name}
            {cat.bill && <span className="rpo-bill">Bill</span>}
          </h2>
        </div>
        <button type="button" className="rpo-close" onClick={onClose} aria-label={`Close ${cat.name} details`}>
          <Icon name="x" />
        </button>
      </header>
      <div className="rpo-panel-body">
        <dl className="rpo-dstats">
          {stats.map((s) => (
            <div key={s.k}>
              <dt>{s.k}</dt>
              <dd className="num">{s.v}</dd>
            </div>
          ))}
        </dl>

        <div className="rpo-trend" role="img" aria-label={trendLabel} style={{ '--n': cols.length } as CSSProperties}>
          <div className="rpo-trend-plot" aria-hidden="true">
            {usual !== null && usual > DUST && (
              <>
                <span className="rpo-trend-avg" style={{ bottom: h(usual) }} />
                <span className="rpo-trend-avg-label num" style={{ bottom: h(usual) }}>
                  usual {short(usual)}
                </span>
              </>
            )}
            {cols.map((c) => (
              <div className="rpo-trend-col" key={c.month}>
                {c.ghost > 0 && <span className="rpo-trend-ghost" style={{ height: h(c.ghost) }} />}
                <span className={`rpo-trend-bar${c.on ? ' is-on' : ''}`} style={{ height: h(c.value) }} />
              </div>
            ))}
          </div>
          <div className="rpo-trend-labels" aria-hidden="true">
            {cols.map((c) => (
              <span key={c.month}>
                <b className="num">{short(c.value)}</b>
                {monthAbbr(c.month)}
              </span>
            ))}
          </div>
        </div>

        <div className={`rpo-note${note.tone ? ` is-${note.tone}` : ''}`}>
          <strong>{note.title}</strong>
          <p>{note.body}</p>
        </div>

        <div>
          <h3>Where, exactly</h3>
          {detail.error && !detail.data ? (
            <p className="rpo-none">
              Couldn’t load the details.{' '}
              <button type="button" className="link-btn" onClick={detail.reload}>
                Try again
              </button>
            </p>
          ) : !detail.data ? (
            <RowsSkeleton />
          ) : merchants.length === 0 ? (
            <p className="rpo-none">Nothing spent here in this period.</p>
          ) : (
            <ul className="rpo-merch-list">
              {merchants.map((m) => (
                <li className="rpo-merch" key={m.name}>
                  <span className="rpo-merch-name">
                    {m.name}{' '}
                    <span>
                      · {cat.bill ? plural(m.count, 'payment') : plural(m.count, 'purchase')}
                      {perMonth && ` in ${v.idx.length} months`}
                    </span>
                  </span>
                  <span className="num rpo-amt">{W(m.spent)}</span>
                  <span className="rpo-merch-bar" aria-hidden="true">
                    <span style={{ width: `${((m.spent / topSpent) * 100).toFixed(1)}%` }} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <h3>Biggest purchases</h3>
          {!detail.data ? (
            !detail.error && <RowsSkeleton />
          ) : detail.data.biggest.length === 0 ? (
            <p className="rpo-none">No purchases in this period.</p>
          ) : (
            <ul className="rpo-tx-list">
              {detail.data.biggest.map((t) => (
                <li className="rpo-tx" key={t.id}>
                  <span className="num">{dayFmt.format(parseISODate(t.date))}</span>
                  <span className="truncate">{t.name}</span>
                  <span className="num rpo-amt">{cents(Math.abs(t.amount))}</span>
                </li>
              ))}
            </ul>
          )}
          <Link className="rpo-all" to={`/transactions?${new URLSearchParams({ cat: cat.id, start, end }).toString()}`}>
            See all transactions →
          </Link>
        </div>
      </div>
    </aside>
  );
}

function RowsSkeleton() {
  return (
    <div className="rpo-skel-rows" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <Skeleton key={i} width="100%" height={16} />
      ))}
    </div>
  );
}
