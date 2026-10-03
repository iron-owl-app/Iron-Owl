import { Link } from 'react-router-dom';
import type { DashboardData } from '../../api';
import { plural } from '../../lib/format';
import { dollars, monthDay, monthLong } from './dashMath';

interface Tile {
  key: string;
  label: string;
  value: string;
  sub: string;
  /** Green text for "Up $X" (always with the word "Up"). */
  good?: boolean;
  to: string;
}

/**
 * The three small stacked cards: Came in this month, Saved toward goals, Investments worth
 * (left out when there are no investment accounts). Each opens its page.
 */
export function StatTiles({ d }: { d: DashboardData }) {
  const failed = 'Couldn’t load this just now.';
  const current = d.months?.find((m) => !m.complete) ?? null;
  const month = current?.month ?? d.today.slice(0, 7);
  const pay = d.budget?.next_paycheck ?? null;

  const tiles: Tile[] = [];

  tiles.push(
    d.months
      ? {
          key: 'in',
          label: 'Came in this month',
          value: dollars(current?.came_in ?? 0),
          sub: pay ? `Next paycheck: ${dollars(pay.amount)} on ${monthDay(pay.date)}` : `Since ${monthLong(month)} 1`,
          to: `/transactions?view=in&start=${month}-01`,
        }
      : { key: 'in', label: 'Came in this month', value: '—', sub: failed, to: `/transactions?view=in&start=${month}-01` },
  );

  const g = d.goals;
  if (!g) {
    tiles.push({ key: 'goals', label: 'Saved toward goals', value: '—', sub: failed, to: '/goals' });
  } else {
    const sub = !g.budget_ready
      ? 'Set up your Budget first'
      : g.in_progress > 0
        ? `${plural(g.in_progress, 'goal')} in progress`
        : g.total_saved > 0.5
          ? 'No goals in progress'
          : 'No goals yet';
    tiles.push({ key: 'goals', label: 'Saved toward goals', value: dollars(g.total_saved), sub, to: '/goals' });
  }

  const inv = d.investments;
  if (!inv) {
    tiles.push({ key: 'inv', label: 'Investments worth', value: '—', sub: failed, to: '/investments' });
  } else if (inv.has_accounts) {
    const c = inv.change;
    const sub =
      c === null
        ? 'New this month'
        : c >= 0.5
          ? `Up ${dollars(c)} since last month`
          : c <= -0.5
            ? `Down ${dollars(-c)} since last month`
            : 'About the same as last month';
    tiles.push({ key: 'inv', label: 'Investments worth', value: dollars(inv.worth), sub, good: c !== null && c >= 0.5, to: '/investments' });
  }

  return (
    <div className="home-tiles">
      <div className="home-tiles-in">
      {tiles.map((t) => (
        <Link key={t.key} to={t.to} className="home-tile">
          <span className="home-tile-top">
            <span className="home-tile-label">{t.label}</span>
            <span className="home-tile-arrow" aria-hidden="true">
              →
            </span>
          </span>
          <span className="home-tile-value num-font">{t.value}</span>
          <span className={`home-tile-sub${t.good ? ' is-good' : ''}`}>{t.sub}</span>
        </Link>
      ))}
      </div>
    </div>
  );
}
