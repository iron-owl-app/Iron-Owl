import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { useLinkBankTo } from '../state';
import { api, type InvestmentAccount, type InvestmentMix, type InvestmentPoint, type Investments } from '../api';
import { useApi } from '../lib/useApi';
import { formatMoney } from '../lib/format';
import { goalMonthLong, goalMonthShort } from '../lib/goals';
import { readPref, writePref } from '../lib/prefs';
import { Icon } from '../components/Icon';
import { Skeleton } from '../components/ui';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { useBankReauth } from '../components/useBankReauth';
import { GrowthChart } from './investments/GrowthChart';
import { InvestmentsDetailed } from './investments/Detailed';
import { fundNames, mixColor, mixWords } from './investments/mix';
import './investments/investments.css';

/*
 * Investments (design D9, layout B "Pick an account"): a row of tiles (All accounts plus each
 * account) drives the summary, the growth chart and "What it holds". Honest numbers only:
 * worth is the account balance; history is real balance snapshots; the changes include any
 * money added (FinTrack doesn't know contributions). Gains are green; losses are plain text
 * with "−", never red. The earlier holdings tables stay behind the "Detailed view" switch.
 */

type Sel = 'all' | number;
type Range = 12 | 36 | 60;

const DETAILED_PREF = 'investments.detailed';
const isBool = (v: unknown): v is boolean => typeof v === 'boolean';
const whole = (v: number) => formatMoney(v, { cents: false });
const signedWhole = (v: number) => formatMoney(v, { cents: false, signed: true });
const pctText = (p: number) => `${p > 0.04 ? '+' : p < -0.04 ? '−' : ''}${Math.abs(p).toFixed(1)}%`;
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
const shortDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const staleDay = (iso: string | null) => (iso ? dayFmt.format(new Date(iso)) : null);

interface View {
  title: string;
  worth: number;
  change: number | null;
  changePct: number | null;
  changeMissing: string[];
  trackedSince: string | null;
  sinceTracking: number | null;
  points: InvestmentPoint[];
  mix: InvestmentMix[];
  holdTitle: string;
  holdSub: string;
  pctOf: 'of this' | 'of the total';
  wholeAccount: boolean;
  /** The bank's holdings add up to more than the balance: say so under "What it holds". */
  holdingsDiffer: boolean;
}

const needsSignIn = (a: InvestmentAccount) => a.connection?.status === 'login_required';

export function InvestmentsPage() {
  const linkTo = useLinkBankTo();
  const inv = useApi(() => api.investments(), []);
  const [sel, setSel] = useState<Sel>('all');
  const [range, setRange] = useState<Range>(12);
  const [detailed, setDetailed] = useState(() => readPref(DETAILED_PREF, false, isBool));
  const reauth = useBankReauth({ onDone: (ok) => ok && inv.reload() });
  const data = inv.data;

  // A picked account that's gone (hidden, removed) falls back to All accounts.
  useEffect(() => {
    if (data && sel !== 'all' && !data.accounts.some((a) => a.id === sel)) setSel('all');
  }, [data, sel]);

  const view = useMemo(() => (data ? viewOf(data, sel) : null), [data, sel]);

  if (inv.error && !data) return <ErrorPanel error={inv.error} onRetry={inv.reload} />;

  // Banks that need a sign-in (one banner each).
  const stale = new Map<number, { itemId: number; kind: NonNullable<InvestmentAccount['connection']>['kind']; bank: string; synced: string | null }>();
  for (const a of data?.accounts ?? []) {
    if (!needsSignIn(a) || !a.connection) continue;
    if (!stale.has(a.connection.item_id)) {
      stale.set(a.connection.item_id, { itemId: a.connection.item_id, kind: a.connection.kind, bank: a.institution_name ?? a.name, synced: a.connection.last_synced_at });
    }
  }

  const empty = data && data.accounts.length === 0;

  return (
    <div className="inv-page">
      <header className="inv-head">
        <div className="inv-head-text">
          <h1>Investments</h1>
          <p>Your retirement, health savings and investment accounts. Numbers update each time Iron Owl updates from your banks.</p>
        </div>
        {data && !empty && (
          <button
            type="button"
            role="switch"
            aria-checked={detailed}
            className="inv-switch"
            onClick={() => {
              setDetailed(!detailed);
              writePref(DETAILED_PREF, !detailed);
            }}
          >
            <span className="inv-switch-track" aria-hidden="true">
              <span className="inv-switch-thumb" />
            </span>
            Detailed view
          </button>
        )}
      </header>

      {[...stale.values()].map((s) => {
        const busy = reauth.busyItemId === s.itemId;
        const when = staleDay(s.synced);
        return (
          <div key={s.itemId} className="inv-signin" role="region" aria-label={`${s.bank} needs you`}>
            <p>
              <strong>{s.bank} needs you to sign in again.</strong> <span>{when ? `Its numbers below are from ${when}.` : 'Its numbers below may be out of date.'}</span>
            </p>
            <button
              type="button"
              className="inv-btn inv-btn-strong"
              onClick={() => reauth.start({ id: s.itemId, kind: s.kind, institution_name: s.bank })}
              aria-disabled={reauth.busyItemId !== null || undefined}
            >
              {busy ? (reauth.phase === 'updating' ? 'Updating…' : 'Opening sign-in…') : 'Sign in again'}
            </button>
          </div>
        );
      })}
      {reauth.launcher}

      {!data ? (
        <Loading />
      ) : empty ? (
        <div className="panel">
          <EmptyState
            kind="investments"
            title="No investment accounts yet"
            actions={
              <>
                <Link to={linkTo} className="btn btn-primary">
                  <Icon name="link" />
                  Add 401(k) · HSA · brokerage
                </Link>
                <Link to="/accounts?new=1" className="btn">
                  <Icon name="plus" />
                  Add manually
                </Link>
              </>
            }
          >
            Link a 401(k), IRA, HSA or brokerage account to see what it’s worth and what it holds, in plain words.
          </EmptyState>
        </div>
      ) : detailed ? (
        <InvestmentsDetailed />
      ) : (
        view && (
          <>
            <Tiles data={data} sel={sel} onPick={setSel} />
            <Summary view={view} range={range} setRange={setRange} firstMonth={data.history.total[0]?.month ?? ''} sel={sel} />
            <Holdings view={view} />
          </>
        )
      )}
    </div>
  );
}

function viewOf(d: Investments, sel: Sel): View {
  const n = d.accounts.length;
  // A picked account that just disappeared (hidden, removed) shows All accounts until the
  // effect above resets the pick.
  const a = sel === 'all' ? undefined : d.accounts.find((x) => x.id === sel);
  if (!a) {
    return {
      title: 'Worth now, all accounts',
      worth: d.total.worth,
      change: d.total.change,
      changePct: d.total.change_pct,
      changeMissing: d.total.change_missing,
      trackedSince: d.total.tracked_since,
      sinceTracking: d.total.change_since_tracking,
      points: d.history.total,
      mix: d.mix,
      holdTitle: 'What your money is in',
      holdSub: n === 1 ? 'Your one account.' : n === 2 ? 'Both accounts together.' : `All ${n} accounts together.`,
      pctOf: n === 1 ? 'of this' : 'of the total',
      wholeAccount: false,
      holdingsDiffer: d.holdings_differ,
    };
  }
  const synced = a.connection?.last_synced_at;
  return {
    title: `${a.name}, worth now`,
    worth: a.worth,
    change: a.change,
    changePct: a.change_pct,
    changeMissing: [],
    trackedSince: a.tracked_since,
    sinceTracking: a.change_since_tracking,
    points: d.history.by_account[String(a.id)] ?? [],
    mix: a.mix,
    holdTitle: `What ${a.name} holds`,
    holdSub: [a.institution_name ?? (a.source === 'manual' ? 'Added by hand' : null), needsSignIn(a) && synced ? `numbers from ${shortDayFmt.format(new Date(synced))}` : null]
      .filter(Boolean)
      .join(' · '),
    pctOf: 'of this',
    wholeAccount: !a.holdings_known,
    holdingsDiffer: a.holdings_differ,
  };
}

// ---------------------------------------------------------------- tiles

function Tiles({ data, sel, onPick }: { data: Investments; sel: Sel; onPick: (s: Sel) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const tiles: { id: Sel; name: string; worth: number; change: number | null; stale: boolean }[] = [
    { id: 'all', name: 'All accounts', worth: data.total.worth, change: data.total.change, stale: false },
    ...data.accounts.map((a) => ({ id: a.id as Sel, name: a.name, worth: a.worth, change: a.change, stale: needsSignIn(a) })),
  ];
  const cur = Math.max(0, tiles.findIndex((t) => t.id === sel));
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    const to = e.key === 'Home' ? 0 : e.key === 'End' ? tiles.length - 1 : step ? (cur + step + tiles.length) % tiles.length : -1;
    if (to < 0) return;
    e.preventDefault();
    onPick(tiles[to]!.id);
    refs.current[to]?.focus();
  }
  return (
    <div className="inv-tiles" role="radiogroup" aria-label="Show" onKeyDown={onKey}>
      {tiles.map((t, i) => {
        const on = i === cur;
        return (
          <button
            key={String(t.id)}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            className={`inv-tile${on ? ' is-on' : ''}`}
            onClick={() => onPick(t.id)}
          >
            <span className="inv-tile-name">
              <span className="inv-tile-label">{t.name}</span>
              {t.stale && (
                <span className="inv-dot-red" role="img" aria-label="needs sign-in" />
              )}
            </span>
            <span className="inv-tile-worth num">{whole(t.worth)}</span>
            {t.change === null ? (
              <span className="inv-tile-change">New this month</span>
            ) : (
              <span className={`inv-tile-change num${t.change > 0.004 ? ' is-up' : ''}`}>{signedWhole(t.change)} this month</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------- summary + chart

function Summary({ view, range, setRange, firstMonth, sel }: { view: View; range: Range; setRange: (r: Range) => void; firstMonth: string; sel: Sel }) {
  const pts = view.points;
  const n = pts.length;
  // Offer only the ranges there's data for (a year needs more than 13 month-ends to be worth a longer view).
  const offered: Range[] = [12, ...(n > 13 ? [36 as Range] : []), ...(n > 37 ? [60 as Range] : [])];
  const r: Range = offered.includes(range) ? range : 12;
  const shown = pts.slice(-(r + 1));
  const up = (v: number | null) => (v !== null && v > 0.004 ? ' is-up' : '');
  const trackedLabel = view.trackedSince ? goalMonthShort(view.trackedSince) : null;
  // Tracking that started last month says the same as "Since last month": only show it when older.
  const showSince = view.trackedSince !== null && view.sinceTracking !== null && n >= 3;

  return (
    <section className="inv-card inv-summary" aria-labelledby="inv-sum-h">
      <div className="inv-sum-top">
        <div>
          <h2 id="inv-sum-h" className="inv-sum-title">
            {view.title}
          </h2>
          <div className="inv-worth num">{formatMoney(view.worth)}</div>
        </div>
        <dl className="inv-stats">
          <div>
            <dt>Since last month</dt>
            {view.change === null ? (
              <dd>New this month</dd>
            ) : (
              <dd className={`num${up(view.change)}`}>
                {signedWhole(view.change)}
                {view.changePct !== null && ` (${pctText(view.changePct)})`}
              </dd>
            )}
            <dd className="inv-stat-note">
              {view.change === null ? 'No number from last month yet' : 'Includes any money added'}
              {view.changeMissing.length > 0 && `. Leaves out ${view.changeMissing.join(', ')} (new this month)`}
            </dd>
          </div>
          {showSince && (
            <div>
              <dt>Since Iron Owl started tracking ({trackedLabel})</dt>
              <dd className={`num${up(view.sinceTracking)}`}>{signedWhole(view.sinceTracking!)}</dd>
              <dd className="inv-stat-note">Includes any money added</dd>
            </div>
          )}
        </dl>
      </div>

      <div className="inv-chart-wrap">
        {n >= 3 && (
          <div className="inv-chart-head">
            <span className="inv-legend">
              <span className="inv-legend-line" aria-hidden="true" />
              Worth at the end of each month
            </span>
            {offered.length > 1 ? (
              <div
                role="radiogroup"
                aria-label="Time"
                className="inv-ranges"
                onKeyDown={(e) => {
                  const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
                  if (!step) return;
                  e.preventDefault();
                  const i = (offered.indexOf(r) + step + offered.length) % offered.length;
                  setRange(offered[i]!);
                  (e.currentTarget.querySelectorAll('button')[i] as HTMLButtonElement | undefined)?.focus();
                }}
              >
                {offered.map((o) => (
                  <button
                    key={o}
                    type="button"
                    role="radio"
                    aria-checked={r === o}
                    tabIndex={r === o ? 0 : -1}
                    className={`inv-range${r === o ? ' is-on' : ''}`}
                    onClick={() => setRange(o)}
                  >
                    {o === 12 ? '1 year' : o === 36 ? '3 years' : '5 years'}
                  </button>
                ))}
              </div>
            ) : (
              <span className="inv-range-note">Since {goalMonthShort(pts[0]!.month)}</span>
            )}
          </div>
        )}
        {n >= 3 ? (
          <GrowthChart key={`${String(sel)}-${r}`} points={shown} firstMonth={firstMonth} />
        ) : (
          <div className="inv-chart-note">
            <p>
              <strong>The growth chart needs a little more time.</strong>{' '}
              {n > 0
                ? `Iron Owl started keeping track in ${goalMonthLong(pts[0]!.month)}. The chart shows up once there are a few months to compare.`
                : 'The chart shows up once Iron Owl has a few months of numbers to compare.'}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------- what it holds

function Holdings({ view }: { view: View }) {
  const rows = view.mix.filter((x) => x.value > 0.004);
  return (
    <section className="inv-card inv-holds" aria-labelledby="inv-hold-h">
      <div>
        <h2 id="inv-hold-h" className="inv-hold-title">
          {view.holdTitle}
        </h2>
        {view.holdSub && <p className="inv-hold-sub">{view.holdSub}</p>}
      </div>
      {rows.length === 0 ? (
        <p className="inv-hold-sub">Nothing to show yet.</p>
      ) : (
        <>
          <div className="inv-mixbar" aria-hidden="true">
            {rows.map((x, i) => (
              <span key={`${x.kind}-${x.year ?? ''}-${i}`} style={{ flex: `${Math.max(x.pct, 0.5)} 1 0`, background: mixColor(x.kind) }} />
            ))}
          </div>
          <ul className="inv-mix-rows">
            {rows.map((x, i) => {
              const w = mixWords(x, view.wholeAccount);
              const names = x.kind === 'not_broken_down' ? '' : fundNames(x.names);
              return (
                <li key={`${x.kind}-${x.year ?? ''}-${i}`} className="inv-mix-row">
                  <span className="inv-swatch" style={{ background: mixColor(x.kind) }} aria-hidden="true" />
                  <span className="inv-mix-text">
                    <span className="inv-mix-name">{w.name}</span>
                    <span className="inv-mix-desc">{w.desc}</span>
                    {names && <span className="inv-mix-funds">{names}</span>}
                  </span>
                  <span className="inv-mix-num">
                    <span className="inv-mix-value num">{formatMoney(x.value)}</span>
                    <span className="inv-mix-pct num">{x.pct > 0 && x.pct < 1 ? `Under 1% ${view.pctOf}` : `${Math.round(x.pct)}% ${view.pctOf}`}</span>
                  </span>
                </li>
              );
            })}
          </ul>
          {view.holdingsDiffer && <p className="inv-hold-sub">Your bank’s numbers don’t quite match; showing what it holds.</p>}
        </>
      )}
    </section>
  );
}

function Loading() {
  return (
    <div aria-busy="true" aria-label="Loading investments" className="inv-loading">
      <div className="inv-tiles">
        {[0, 1, 2].map((i) => (
          <div className="inv-tile" key={i}>
            <Skeleton width="50%" height={14} />
            <Skeleton width="70%" height={22} />
            <Skeleton width="40%" height={12} />
          </div>
        ))}
      </div>
      <div className="inv-card">
        <Skeleton width="30%" height={14} />
        <Skeleton width="45%" height={40} />
        <Skeleton width="100%" height={220} />
      </div>
    </div>
  );
}
