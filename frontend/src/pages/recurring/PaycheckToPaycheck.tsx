import { useId, useMemo } from 'react';
import type { ForecastCalendar, ForecastOccurrence, Paychecks } from '../../api';
import { Icon } from '../../components/Icon';
import {
  coversText,
  dollars,
  leftText,
  md,
  NO_PAYCHECK_TEXT,
  payPeriods,
  perDayText,
  periodTitle,
  pickPaycheckSeries,
  rowStatusText,
  type PayPeriod,
} from './payPeriods';
import './panels.css';

/**
 * Paycheck to paycheck (README §6, SPEC "Numbers" › Paychecks): the current pay period and the
 * next two, with the bills each paycheck has to cover and what's left for everyday spending.
 */
export function PaycheckToPaycheck({
  cal,
  paychecks,
  onOpen,
  onAddPaycheck,
}: {
  cal: ForecastCalendar;
  /** `/api/paychecks` (null while loading or when it failed: the largest income series is used). */
  paychecks: Paychecks | null;
  onOpen: (o: ForecastOccurrence) => void;
  /** Opens Add with type Paycheck. */
  onAddPaycheck: () => void;
}) {
  const headId = useId();
  const periods = useMemo(() => payPeriods(cal, pickPaycheckSeries(cal, paychecks)), [cal, paychecks]);
  return (
    <section className="calx-card calx-pay" aria-labelledby={headId}>
      <h2 id={headId} className="calx-h2">
        Paycheck to paycheck
      </h2>
      {periods.length === 0 ? (
        <div className="calx-pay-empty">
          <p>{NO_PAYCHECK_TEXT}</p>
          <button type="button" className="btn btn-primary calx-btn" onClick={onAddPaycheck}>
            <Icon name="plus" />
            Add your paycheck
          </button>
        </div>
      ) : (
        <>
          <p className="calx-sub">What each paycheck has to cover before the next one. Card charges are paid by the card payment, so they aren’t listed twice.</p>
          <div className="calx-pay-grid">
            {periods.map((p) => (
              <Period key={p.start} p={p} onOpen={onOpen} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function Period({ p, onOpen }: { p: PayPeriod; onOpen: (o: ForecastOccurrence) => void }) {
  const short = p.left < 0;
  return (
    <article className={`calx-period${p.isNow ? ' is-now' : ''}`} aria-label={`${periodTitle(p)}${p.isNow ? ', now' : ''}`}>
      <header className="calx-period-head cal-k-in">
        {p.paycheck ? (
          <button type="button" className="calx-period-headbtn" onClick={() => onOpen(p.paycheck!)}>
            <HeadText p={p} />
          </button>
        ) : (
          <div className="calx-period-headbtn is-static">
            <HeadText p={p} />
          </div>
        )}
      </header>
      {p.rows.length === 0 ? (
        <p className="calx-period-none">No bills from checking in these days.</p>
      ) : (
        <ul className="calx-period-rows">
          {p.rows.map((r) => (
            <li key={r.occ.key}>
              <button type="button" className="calx-period-row" onClick={() => onOpen(r.occ)}>
                <span className="calx-period-date">{md(r.occ.date)}</span>
                <span className="calx-period-what">
                  <span className="calx-period-name">{r.occ.name}</span>
                  <span className={`calx-st is-${r.status}`}>{rowStatusText(r)}</span>
                </span>
                <span className={`calx-period-amt num${r.type === 'income' ? ' is-in' : ''}`}>
                  {r.type === 'income' ? '+' : ''}
                  {dollars(r.amount)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <footer className="calx-period-foot">
        {p.otherIncome > 0 && (
          <div className="calx-foot-line">
            <span>Other money in</span>
            <span className="num">+{dollars(p.otherIncome)}</span>
          </div>
        )}
        <div className="calx-foot-line">
          <span>Bills</span>
          <span className="num">{dollars(p.bills)}</span>
        </div>
        <div className="calx-foot-line calx-foot-left">
          <strong>Left for everyday spending</strong>
          <strong className={`num ${short ? 'is-short' : 'is-left'}`}>{leftText(p)}</strong>
        </div>
        <p className="calx-foot-day">{perDayText(p)}</p>
      </footer>
    </article>
  );
}

function HeadText({ p }: { p: PayPeriod }) {
  return (
    <>
      <span className="calx-period-title">
        <span>{periodTitle(p)}</span>
        {p.isNow && <span className="calx-now">Now</span>}
      </span>
      <span className="calx-period-covers">{coversText(p)}</span>
    </>
  );
}
