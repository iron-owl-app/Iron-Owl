import { useId, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { DashboardData, HomeBank } from '../../api';
import { formatMoney } from '../../lib/format';
import { sinceWhen, withMask } from './homeFormat';

const isStale = (b: HomeBank | undefined) => !!b && (b.status === 'login_required' || b.status === 'error');
/** Rows before "N more →". */
const MAX_ROWS = 4;

/** "Money you have": the checking and savings total, then one row per account (each opens its Accounts page). */
export function MoneyCard({ cash, banks, error }: { cash: DashboardData['cash']; banks: HomeBank[]; error?: ReactNode }) {
  const headId = useId();
  const bankOf = (itemId: number | null) => (itemId === null ? undefined : banks.find((b) => b.item_id === itemId));

  let body: ReactNode | [ReactNode, ReactNode];
  if (!cash) {
    body = error;
  } else if (cash.accounts.length === 0) {
    body = [
      <div key="head">
        <div className="home-money-total num-font">{formatMoney(0)}</div>
        <p className="home-card-note">No checking or savings accounts yet.</p>
      </div>,
      <Link key="add" to="/accounts?new=1" className="home-link home-money-add">
        Add an account<span aria-hidden="true"> →</span>
      </Link>,
    ];
  } else {
    const rows = cash.accounts.slice(0, MAX_ROWS);
    const more = cash.accounts.length - rows.length;
    body = [
      <div key="head">
        <div className="home-money-total num-font">{formatMoney(cash.total)}</div>
        <p className="home-card-note">In checking and savings</p>
      </div>,
      <ul key="list" className="home-inset home-accts">
          {rows.map((a) => {
            const b = bankOf(a.item_id);
            const stale = a.source === 'plaid' && isStale(b);
            const sub =
              a.source === 'manual'
                ? 'You update this one yourself'
                : stale
                  ? `Not updated since ${sinceWhen(b!.last_synced_at)} · ${b!.status === 'login_required' ? 'sign in again to update' : 'the bank had a problem'}`
                  : (a.institution_name ?? b?.institution_name ?? '');
            return (
              <li key={a.id}>
                <Link to={`/accounts/${a.id}`} className="home-acct">
                  <span className="home-acct-text">
                    <span className="home-acct-name">{withMask(a.name, a.mask)}</span>
                    {sub && <span className={`home-acct-sub${stale ? ' is-stale' : ''}`}>{sub}</span>}
                  </span>
                  <span className="home-acct-bal num-font">{formatMoney(a.balance)}</span>
                </Link>
              </li>
            );
          })}
          {more > 0 && (
            <li>
              <Link to="/accounts" className="home-acct home-acct-more">
                <span>
                  {more} more<span aria-hidden="true"> →</span>
                  <span className="sr-only"> {more === 1 ? 'account' : 'accounts'}</span>
                </span>
              </Link>
            </li>
          )}
      </ul>,
    ];
  }

  return (
    <section className="home-card home-money" aria-labelledby={headId}>
      {/* The label, the total and its note sit together; the account list is pinned to the bottom. */}
      <div className="home-money-head">
        <h2 id={headId} className="home-card-label">
          Money you have
        </h2>
        {Array.isArray(body) ? body[0] : body}
      </div>
      {Array.isArray(body) ? body[1] : null}
    </section>
  );
}
