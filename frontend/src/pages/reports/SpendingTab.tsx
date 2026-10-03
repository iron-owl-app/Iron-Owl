import { useEffect, useId, type CSSProperties } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, ApiError, type SpendingReport, type SpendingReportCategory } from '../../api';
import { useApi } from '../../lib/useApi';
import { MonthStepper, monthYearLabel } from '../../components/MonthStepper';
import { MonthBars } from '../../components/MonthBars';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import {
  averageNote,
  barsFor,
  categoryChange,
  dayShort,
  money,
  monthLong,
  moreText,
  shareText,
  signedCents,
  storeSub,
  storesHeading,
  storesLeftOutNote,
  summaryComparison,
  summaryTitle,
  transactionsLink,
} from './spendingMath';
import './spending.css';

/**
 * Reports › Spending (reports-spending design, layout A): one month at a time
 * (`?month=YYYY-MM`, `&cat=<id>` opens a category). Looks back only; Budget is where the user plans.
 */

const isMonth = (v: string | null): v is string => !!v && /^\d{4}-(0[1-9]|1[0-2])$/.test(v);
const catStyle = (hue: number) => ({ '--h': hue }) as CSSProperties;

export function SpendingTab() {
  const [params, setParams] = useSearchParams();
  const rawMonth = params.get('month');
  const month = isMonth(rawMonth) ? rawMonth : null;
  const open = params.get('cat');
  const report = useApi(() => api.spendingReport(month ?? undefined), [month]);
  const r = report.data;

  const setParam = (next: { month?: string | null; cat?: string | null }) => {
    setParams(
      (p) => {
        const q = new URLSearchParams(p);
        q.set('tab', 'spending');
        if (next.month !== undefined) {
          if (next.month) q.set('month', next.month);
          else q.delete('month');
          q.delete('cat'); // a new month closes the open category
        }
        if (next.cat !== undefined) {
          if (next.cat) q.set('cat', next.cat);
          else q.delete('cat');
        }
        return q;
      },
      { replace: true },
    );
  };

  // A month outside what FinTrack has (an old link): show today's month instead.
  const outside = report.error instanceof ApiError && report.error.status === 422 && month !== null;
  useEffect(() => {
    if (outside) setParam({ month: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outside]);

  if (report.error && !r) {
    if (outside) return <SpendingSkeleton />;
    return <ErrorPanel error={report.error} onRetry={report.reload} />;
  }
  if (!r) return <SpendingSkeleton />;

  const shown = r.month;
  const empty = !r.first_data_date;
  return (
    <div className={`sp${report.loading ? ' is-loading' : ''}`} aria-busy={report.loading || undefined}>
      <div className="sp-top">
        <MonthStepper month={shown} min={r.range.first} max={r.range.last} onChange={(m) => setParam({ month: m === r.current ? null : m })} />
      </div>
      {empty ? (
        <section className="rp-card sp-empty">
          <h2>Nothing to show yet</h2>
          <p className="rp-card-sub">Spending fills in as purchases arrive from your bank and card accounts.</p>
        </section>
      ) : (
        <>
          <div className="sp-row">
            <SummaryCard r={r} />
            <SixMonths r={r} onPick={(m) => setParam({ month: m === r.current ? null : m })} />
          </div>
          <div className="sp-row sp-row-2">
            <ByCategory r={r} open={open} onToggle={(id) => setParam({ cat: open === id ? null : id })} />
            <Stores r={r} />
          </div>
        </>
      )}
    </div>
  );
}

function SpendingSkeleton() {
  return (
    <div className="sp" aria-busy="true">
      <div className="sp-row">
        <div className="rp-card sp-summary">
          <Skeleton width={180} height={18} />
          <Skeleton width={220} height={44} />
          <Skeleton width="80%" height={18} />
        </div>
        <div className="rp-card sp-months">
          <Skeleton width="100%" height={190} style={{ borderRadius: 18 }} />
        </div>
      </div>
      <div className="rp-card">
        <Skeleton width="100%" height={260} style={{ borderRadius: 18 }} />
      </div>
    </div>
  );
}

function SummaryCard({ r }: { r: SpendingReport }) {
  const cmp = summaryComparison(r);
  return (
    <section className="rp-card sp-summary" aria-label={summaryTitle(r)}>
      <span className="sp-summary-title">{summaryTitle(r)}</span>
      <span className="sp-total num">{money(r.summary.total, true)}</span>
      <span className={`sp-cmp is-${cmp.tone}`}>{cmp.text}</span>
      <span className="sp-foot">Doesn’t count money moved between your accounts or to savings.</span>
    </section>
  );
}

function SixMonths({ r, onPick }: { r: SpendingReport; onPick: (month: string) => void }) {
  const headId = useId();
  const { bars, averagePct } = barsFor(r);
  const avg = r.average;
  return (
    <section className="rp-card sp-months" aria-labelledby={headId}>
      <div className="rp-card-head">
        <div>
          <h2 id={headId}>The last 6 months</h2>
          <p className="rp-card-sub">Tap a month to see it</p>
        </div>
        {avg && (
          <div className="sp-legend" aria-hidden="true">
            <span>
              <i className="is-under" />
              Under average
            </span>
            <span>
              <i className="is-over" />
              Over average
            </span>
            <span>
              <i className="is-avg" />
              Average
            </span>
          </div>
        )}
      </div>
      <MonthBars
        label="The last 6 months. Pick a month to see it."
        bars={bars.map((b) => ({ key: b.month, short: b.short, value: b.value, sub: b.sub, heightPct: b.heightPct, tone: b.tone, ariaLabel: b.ariaLabel, disabled: b.month < r.range.first }))}
        picked={r.summary.month}
        onPick={onPick}
        average={avg && averagePct !== null ? { pct: averagePct, label: `Avg ${money(avg.amount)}` } : null}
      />
      <p className="sp-avg-note">{averageNote(r)}</p>
    </section>
  );
}

function ByCategory({ r, open, onToggle }: { r: SpendingReport; open: string | null; onToggle: (id: string) => void }) {
  const headId = useId();
  const max = Math.max(0, ...r.categories.map((c) => c.amount)) || 1;
  const name = monthLong(r.summary.month);
  return (
    <section className="rp-card sp-cats" aria-labelledby={headId}>
      <div>
        <h2 id={headId}>By category</h2>
        <p className="rp-card-sub">{r.categories.length ? 'Tap one to see what you bought' : `Nothing spent in ${name}${r.summary.month === r.current ? ' yet' : ''}.`}</p>
      </div>
      {r.categories.length > 0 && (
        <ul className="rp-inset sp-cat-list">
          {r.categories.map((c) => (
            <CategoryRow key={c.id} c={c} r={r} max={max} open={open === c.id} onToggle={() => onToggle(c.id)} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CategoryRow({ c, r, max, open, onToggle }: { c: SpendingReportCategory; r: SpendingReport; max: number; open: boolean; onToggle: () => void }) {
  const uid = useId();
  const chg = categoryChange(c, r);
  const share = shareText(c.share, c.amount);
  return (
    <li className={`sp-cat${open ? ' is-open' : ''}`} style={catStyle(c.hue)}>
      <button type="button" className="sp-cat-btn" aria-expanded={open} aria-controls={open ? `${uid}-p` : undefined} onClick={onToggle}>
        <span className="sp-swatch" aria-hidden="true" />
        <span className="sp-cat-mid">
          <span className="sp-cat-top">
            <span className="sp-cat-name">{c.name}</span>
            {chg.text && <span className={`sp-cat-chg is-${chg.tone}`}>{chg.text}</span>}
          </span>
          <span className="sp-bar" aria-hidden="true">
            <span style={{ width: `${((Math.max(0, c.amount) / max) * 100).toFixed(1)}%` }} />
          </span>
        </span>
        <span className="sp-cat-amt">
          <span className="num">{money(c.amount, true)}</span>
          <span className="sp-cat-share">{share}</span>
        </span>
        <span className="sp-chev" aria-hidden="true">
          ›
        </span>
      </button>
      {open && <Purchases id={`${uid}-p`} c={c} r={r} />}
    </li>
  );
}

function Purchases({ id, c, r }: { id: string; c: SpendingReportCategory; r: SpendingReport }) {
  const planLink = c.in_budget && c.kind === 'spending';
  return (
    <div id={id} className="sp-pur" role="region" aria-label={`${c.name}: what you bought in ${monthLong(r.summary.month)}`}>
      {c.purchases.length === 0 ? (
        <p className="rp-note">No purchases to list.</p>
      ) : (
        <ul className="sp-pur-list">
          {c.purchases.map((p) => (
            <li key={`${p.id}-${p.split ? 's' : ''}${p.amount}`} className="sp-pur-row">
              <span className="sp-pur-date">{dayShort(p.date)}</span>
              <span className="sp-pur-name">
                <span className="sp-pur-store">{p.name}</span>
                {(p.pending || p.split || p.amount < 0) && (
                  <span className="sp-pur-tags">
                    {p.amount < 0 && <span className="sp-tag">Refund</span>}
                    {p.pending && <span className="sp-tag">Not final yet</span>}
                    {p.split && <span className="sp-tag">Part of a split</span>}
                  </span>
                )}
              </span>
              <span className={`sp-pur-amt num${p.amount < 0 ? ' is-refund' : ''}`}>{signedCents(p.amount)}</span>
            </li>
          ))}
        </ul>
      )}
      {c.more > 0 && <p className="rp-note">{moreText(c.more)} (see them all in Transactions)</p>}
      <div className="sp-pur-links">
        <Link className="rp-link" to={transactionsLink(c.id, r.summary.month, r.today)}>
          See these in Transactions <span aria-hidden="true">&nbsp;→</span>
        </Link>
        {planLink && (
          <Link className="rp-link" to={`/spending?${new URLSearchParams({ cat: c.id }).toString()}`}>
            See this month’s plan in Budget <span aria-hidden="true">&nbsp;→</span>
          </Link>
        )}
      </div>
    </div>
  );
}

function Stores({ r }: { r: SpendingReport }) {
  const headId = useId();
  const top = r.stores[0]?.amount || 1;
  const left = storesLeftOutNote(r.stores_left_out);
  return (
    <section className="rp-card sp-stores" aria-labelledby={headId}>
      <div>
        <h2 id={headId}>Where you spent the most</h2>
        <p className="rp-card-sub">{storesHeading(r.stores_left_out)}</p>
      </div>
      {r.stores.length === 0 ? (
        <p className="rp-note">No purchases in {monthYearLabel(r.summary.month)}.</p>
      ) : (
        <ol className="sp-store-list">
          {r.stores.map((s) => (
            <li key={s.name} className="sp-store" style={catStyle(s.category?.hue ?? 268)}>
              <span className="sp-store-top">
                <span className="sp-store-name">
                  <span className="sp-store-title">{s.name}</span>
                  <span className="sp-store-sub">{storeSub(s)}</span>
                </span>
                <span className="sp-store-amt num">{money(s.amount, true)}</span>
              </span>
              <span className="sp-bar is-thin" aria-hidden="true">
                <span style={{ width: `${((Math.max(0, s.amount) / top) * 100).toFixed(1)}%` }} />
              </span>
            </li>
          ))}
        </ol>
      )}
      {left && <p className="rp-note">{left}</p>}
    </section>
  );
}
