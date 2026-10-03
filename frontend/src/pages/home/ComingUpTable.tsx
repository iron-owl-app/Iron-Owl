import { useId, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { ComingUpRow, DashboardComingUp } from '../../api';
import { formatMoney } from '../../lib/format';
import { comingUpNote, rowBelowWarning } from './dashMath';
import { fullDay, weekdayShort } from './homeFormat';

function source(r: ComingUpRow): string {
  const base =
    r.kind === 'in'
      ? 'Into checking'
      : r.kind === 'card'
        ? r.account_name
          ? `On ${r.account_name}`
          : 'On your credit card'
        : r.kind === 'plan'
          ? 'Spending plan'
          : 'From checking';
  if (r.status === 'late') return `${base} · Late, still expected`;
  if (r.status === 'pending') return `${base} · Pending at the bank`;
  return base;
}

/**
 * "Coming up": the next 7 days of bills, paychecks and Spending plans, with what checking will
 * have after each one, and a sentence about the lowest point and the warning amount.
 */
export function ComingUpTable({ c, today, error }: { c: DashboardComingUp | null; today: string; error?: ReactNode }) {
  const headId = useId();

  let body: ReactNode;
  if (!c) {
    body = error;
  } else if (!c.account) {
    body = (
      <div className="home-inset home-coming-empty">
        <p>Pick your checking account in Bills and paychecks, and this card will show your bills and what checking will have after each one.</p>
        <Link to="/recurring" className="home-btn home-btn-outline">
          Choose your checking account
        </Link>
      </div>
    );
  } else {
    const note = comingUpNote(c, today);
    body = (
      <>
        {c.rows.length === 0 ? (
          <p className="home-inset home-coming-empty">Nothing is due in the next 7 days.</p>
        ) : (
          <div className="home-inset home-coming" role="table" aria-labelledby={headId}>
            <div className="home-coming-row home-coming-headrow" role="row">
              <span role="columnheader">Date</span>
              <span role="columnheader">What</span>
              <span role="columnheader" className="is-num">
                Amount
              </span>
              <span role="columnheader" className="is-num home-coming-after-head">
                Checking after
              </span>
            </div>
            {c.rows.map((r) => {
              const isIn = r.amount > 0;
              const low = rowBelowWarning(r, c);
              return (
                <div className="home-coming-row" role="row" key={r.key}>
                  <span role="cell" className="home-coming-date">
                    <span className="sr-only">{fullDay(r.date)}</span>
                    <span aria-hidden="true" className="home-coming-dow">
                      {weekdayShort(r.date)}
                    </span>
                    <span aria-hidden="true" className="home-coming-day num-font">
                      {Number(r.date.slice(8, 10))}
                    </span>
                  </span>
                  <span role="cell" className="home-coming-what">
                    <span className="home-coming-name">{r.name}</span>
                    <span className="home-coming-sub">{source(r)}</span>
                  </span>
                  <span role="cell" className="is-num home-coming-amt">
                    <span className={`home-coming-money num-font${isIn ? ' is-in' : ''}`}>
                      {isIn ? '+' : ''}
                      {formatMoney(Math.abs(r.amount))}
                    </span>
                    <span className="home-coming-dir">{isIn ? 'Coming in' : 'Going out'}</span>
                  </span>
                  <span role="cell" className={`is-num home-coming-after${r.after === null ? ' is-none' : ''}${low ? ' is-low' : ''}`}>
                    {r.after !== null && <span className="home-coming-after-label">Checking after: </span>}
                    {r.after === null ? 'Not from checking' : formatMoney(r.after)}
                    {low && <span className="home-coming-lowlabel">Below your warning</span>}
                  </span>
                </div>
              );
            })}
          </div>
        )}
        {c.more > 0 && (
          <Link to="/recurring" className="home-link">
            {c.more} more in Bills and paychecks<span aria-hidden="true"> →</span>
          </Link>
        )}
        {note && <p className="home-card-foot">{note}</p>}
      </>
    );
  }

  return (
    <section className="home-card home-coming-card" aria-labelledby={headId}>
      <div className="home-card-head">
        <div>
          <h2 id={headId} className="home-card-title">
            Coming up
          </h2>
          <p className="home-card-sub">The next 7 days, and what checking will have after each one</p>
        </div>
        <Link to="/recurring" className="home-link">
          Bills and paychecks<span aria-hidden="true"> →</span>
        </Link>
      </div>
      {body}
    </section>
  );
}
