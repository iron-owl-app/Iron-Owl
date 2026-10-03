import { useId, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { DashboardGoals } from '../../api';
import { dollars } from './dashMath';

/** "Goals": up to 3 unfinished goals with "$saved of $target" and a bar; or "Start your first goal". */
export function GoalsMini({ goals, error }: { goals: DashboardGoals | null; error?: ReactNode }) {
  const headId = useId();

  let body: ReactNode;
  if (!goals) {
    body = error;
  } else if (goals.top.length === 0) {
    body = (
      <>
        {goals.total_saved > 0.5 && <p className="home-card-note">Every goal has reached its amount.</p>}
        <Link to="/goals" className="home-link home-link-lg">
          {goals.total_saved > 0.5 ? 'Start a new goal' : 'Start your first goal'}
          <span aria-hidden="true"> →</span>
        </Link>
      </>
    );
  } else {
    body = (
      <ul className="home-goals">
        {goals.top.map((g) => {
          const pct = g.target > 0 ? Math.min(100, Math.max(0, Math.round((g.saved / g.target) * 100))) : 0;
          return (
            <li key={g.id}>
              <div className="home-goal-line">
                <span className="home-goal-name">{g.name}</span>
                <span className="home-goal-amt">
                  {dollars(g.saved)} of {dollars(g.target)}
                </span>
              </div>
              <div className="home-goal-bar" aria-hidden="true">
                <span style={{ width: `${pct}%` }} />
              </div>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section className="home-card home-goals-card" aria-labelledby={headId}>
      <div className="home-card-head">
        <h2 id={headId} className="home-card-title">
          Goals
        </h2>
        {goals && goals.top.length > 0 && (
          <Link to="/goals" className="home-link">
            All goals<span aria-hidden="true"> →</span>
          </Link>
        )}
      </div>
      {body}
    </section>
  );
}
