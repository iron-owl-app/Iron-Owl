import { useId, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { DashboardBudget } from '../../api';
import { formatMoney } from '../../lib/format';
import { daysLeftLabel, monthLong, perDayLine, planPercent } from './dashMath';
import { dollars } from './homeFormat';

/**
 * The highlighted card: "Left to spend in {Month}", the big number, about how much a day, the
 * plan bar and the two pill buttons. Over plan says "Over by $X" with a full bar, never red.
 */
export function LeftToSpendCard({ budget, today, error }: { budget: DashboardBudget | null; today: string; error?: ReactNode }) {
  const headId = useId();
  const monthName = budget ? monthLong(budget.month) : new Intl.DateTimeFormat(undefined, { month: 'long' }).format(new Date());

  let body: ReactNode;
  if (!budget) {
    body = error;
  } else if (!budget.has_budget) {
    body = (
      <>
        <p className="home-hl-lead">Plan what you want to spend this month, and this card will show what’s left.</p>
        <div className="home-hl-actions">
          <Link to="/spending" className="home-pill home-pill-solid">
            Set up your budget
          </Link>
        </div>
      </>
    );
  } else {
    const over = budget.left < -0.004;
    const pct = planPercent(budget.spent, budget.planned);
    body = (
      <>
        <div>
          <div className={`home-hl-amt num-font${over ? ' is-over' : ''}`}>{over ? `Over by ${formatMoney(-budget.left)}` : formatMoney(budget.left)}</div>
          <p className="home-hl-lead">{perDayLine(budget, today)}</p>
        </div>
        <div className="home-hl-plan">
          <div
            className="home-hl-bar"
            role="progressbar"
            aria-label={`${monthName} plan used`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.min(100, pct)}
            aria-valuetext={`${dollars(budget.spent)} spent of ${dollars(budget.planned)} planned, ${pct}%`}
          >
            <span style={{ width: `${over ? 100 : Math.min(100, pct)}%` }} />
          </div>
          <div className="home-hl-legend">
            <span>{dollars(budget.spent)} spent</span>
            <span>
              of {dollars(budget.planned)} planned · {pct}%
            </span>
          </div>
        </div>
        <div className="home-hl-actions">
          <Link to="/spending" className="home-pill home-pill-solid">
            See my budget
          </Link>
          <Link to="/recurring" className="home-pill home-pill-outline">
            See my bills
          </Link>
        </div>
      </>
    );
  }

  return (
    <section className="home-hl" aria-labelledby={headId}>
      <div className="home-hl-top">
        <h2 id={headId}>Left to spend in {monthName}</h2>
        {budget?.has_budget && <span>{daysLeftLabel(budget.days_left)}</span>}
      </div>
      {body}
    </section>
  );
}
