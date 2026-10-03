import { useEffect, useId, useMemo, useState, type CSSProperties, type FormEvent } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { api, ApiError, errorMessage, type Account } from '../../api';
import { useApp, useUpdated } from '../../state';
import { formatMoney, formatRate, initialsFor, plural } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import { useAccounts } from './AccountsShell';
import {
  accountChip,
  balanceWord,
  groupAccounts,
  groupMeta,
  isOverdrawn,
  loansChip,
  rowSubline,
  shownAmount,
  type AccountGroup,
  type Chip,
  type ListRow,
} from './accountGroups';

/**
 * Debts show what's owed as a plain amount (no minus sign, no red; a credit balance as its size).
 * Everything else keeps its sign: an overdrawn account is "−$50.00" with the word "overdrawn".
 */
export const shownBalance = (a: Account) => formatMoney(shownAmount(a), { currency: a.currency });

/** The small word under a row's amount; "overdrawn" is amber. */
function BalanceWord({ a }: { a: Account }) {
  return <span className={`ac-row-word${isOverdrawn(a) ? ' is-amber' : ''}`}>{balanceWord(a)}</span>;
}

export const hueStyle = (hue: number) => ({ '--h': hue }) as CSSProperties;

/** The list (layout A): group cards in two columns, the hidden accounts, and what the user has minus what they owe. */
export function AccountsList() {
  const { accounts, error, reload, openAdd, hiddenOpen, setHiddenOpen } = useAccounts();
  const location = useLocation();
  const view = useMemo(() => (accounts ? groupAccounts(accounts) : null), [accounts]);
  const hiddenId = useId();

  // Back from an account ("← All accounts"): put focus on its row again.
  const from = (location.state as { from?: number } | null)?.from;
  useEffect(() => {
    if (!view || typeof from !== 'number') return;
    // After Layout moves focus to the page (parent effects run after this one).
    const h = requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>(`[data-acct="${from}"]`);
      if (el) {
        el.focus({ preventScroll: true });
        el.scrollIntoView({ block: 'center' });
      }
    });
    return () => cancelAnimationFrame(h);
  }, [view, from]);

  if (!view) {
    if (error) return <ErrorPanel error={error} onRetry={reload} title="Couldn’t load your accounts" />;
    return (
      <div className="ac-columns" role="status" aria-label="Loading your accounts">
        {[0, 1, 2].map((i) => (
          <div key={i} className="ac-group">
            <Skeleton height={220} style={{ borderRadius: 22, display: 'block' }} />
          </div>
        ))}
      </div>
    );
  }

  if (accounts!.length === 0) {
    return (
      <section className="ac-card ac-empty" aria-labelledby="ac-empty-h">
        <h2 id="ac-empty-h">No accounts yet</h2>
        <p>Connect your bank so balances and purchases come in on their own, or add an account yourself and type in its balance.</p>
        <div className="ac-actions">
          <button type="button" className="ac-btn ac-btn-primary" onClick={() => openAdd({ step: 'choose' })}>
            Connect a bank
          </button>
          <button type="button" className="ac-btn" onClick={() => openAdd({ step: 'manual' })}>
            Add one yourself
          </button>
        </div>
      </section>
    );
  }

  return (
    <div className="ac-list">
      {view.groups.length > 0 && (
        <div className="ac-columns">
          {view.groups.map((g) => (
            <GroupCard key={g.id} group={g} />
          ))}
        </div>
      )}

      {view.hidden.length > 0 && (
        <section className="ac-hidden" aria-label="Hidden accounts">
          <button type="button" className="ac-hidden-toggle" aria-expanded={hiddenOpen} aria-controls={hiddenId} onClick={() => setHiddenOpen(!hiddenOpen)}>
            <span className="ac-hidden-text">
              <span className="ac-hidden-title">Hidden accounts · {view.hidden.length}</span>
              <span className="ac-muted">They don’t count in any totals.</span>
            </span>
            <span className="ac-hidden-btn">{hiddenOpen ? 'Hide list' : 'Show'}</span>
          </button>
          {hiddenOpen && (
            <ul className="ac-rows ac-rows-hidden" id={hiddenId}>
              {view.hidden.map((a) => (
                <li key={a.id}>
                  <Link to={`/accounts/${a.id}`} className="ac-row is-hidden" data-acct={a.id}>
                    <span className="ac-avatar" aria-hidden="true">
                      {initialsFor(a.name)}
                    </span>
                    <span className="ac-row-text">
                      <span className="ac-row-name">{a.name}</span>
                      <span className="ac-row-sub">{[a.institution_name, a.mask ? `··${a.mask}` : null].filter(Boolean).join(' ') || groupMeta(a.category).name} · hidden</span>
                    </span>
                    <span className="ac-row-amt">
                      <span className="ac-row-bal">{shownBalance(a)}</span>
                      {isOverdrawn(a) && <BalanceWord a={a} />}
                    </span>
                    <span className="ac-chev" aria-hidden="true">
                      ›
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {view.visibleCount > 0 && (
        <p className="ac-networth">
          What you have minus what you owe: <strong className="ac-num">{formatMoney(view.netWorth)}</strong>
        </p>
      )}
    </div>
  );
}

function GroupCard({ group: g }: { group: AccountGroup }) {
  const headId = useId();
  return (
    <section className="ac-group ac-card" aria-labelledby={headId} style={hueStyle(g.hue)}>
      <header className="ac-group-head">
        <span className="ac-group-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <path d={g.icon} />
          </svg>
        </span>
        <span className="ac-group-names">
          <h2 id={headId} className="ac-group-name">
            {g.name}
          </h2>
          <span className="ac-muted">{plural(g.accounts.length, 'account')}</span>
        </span>
        <span className="ac-group-total">
          <span className="ac-group-total-label">{g.totalLabel}</span>
          <span className="ac-num ac-group-total-num">{formatMoney(g.total)}</span>
        </span>
      </header>
      <ul className="ac-rows">
        {g.rows.map((r) => (
          <li key={r.type === 'account' ? r.account.id : r.key}>{r.type === 'account' ? <AccountRow account={r.account} /> : <LoansRow row={r} />}</li>
        ))}
      </ul>
    </section>
  );
}

function ChipPill({ chip }: { chip: Chip | null }) {
  if (!chip) return null;
  return <span className={`ac-chip is-${chip.tone}`}>{chip.text}</span>;
}

function AccountRow({ account: a }: { account: Account }) {
  const { now } = useUpdated();
  return (
    <Link to={`/accounts/${a.id}`} className="ac-row" data-acct={a.id}>
      <span className="ac-avatar" aria-hidden="true">
        {initialsFor(a.name)}
      </span>
      <span className="ac-row-text">
        <span className="ac-row-name">{a.name}</span>
        <span className="ac-row-sub">{rowSubline(a, now)}</span>
        <ChipPill chip={accountChip(a)} />
      </span>
      <span className="ac-row-amt">
        <span className="ac-row-bal">{shownBalance(a)}</span>
        <BalanceWord a={a} />
      </span>
      <span className="ac-chev" aria-hidden="true">
        ›
      </span>
    </Link>
  );
}

/** Several loans from one lender: one row that opens the loans under it (and can be renamed). */
function LoansRow({ row }: { row: Extract<ListRow, { type: 'loans' }> }) {
  const { openGroups, toggleGroup } = useAccounts();
  const open = openGroups.has(row.key);
  const listId = useId();
  const { now } = useUpdated();
  return (
    <div className={`ac-loans${open ? ' is-open' : ''}`}>
      <button type="button" className="ac-row ac-row-group" aria-expanded={open} aria-controls={listId} onClick={() => toggleGroup(row.key)}>
        <span className="ac-avatar" aria-hidden="true">
          {initialsFor(row.title)}
        </span>
        <span className="ac-row-text">
          <span className="ac-row-name">{row.title}</span>
          <span className="ac-row-sub">
            {row.lender} · {plural(row.accounts.length, 'loan')} · {row.manual ? 'you add these' : 'updates on its own'}
          </span>
          <ChipPill chip={loansChip(row)} />
        </span>
        <span className="ac-row-amt">
          <span className="ac-row-bal">{formatMoney(row.total)}</span>
          <span className="ac-row-word">owed</span>
        </span>
        <span className="ac-chev is-turn" aria-hidden="true">
          ›
        </span>
      </button>
      {open && (
        <div className="ac-kids" id={listId}>
          <ul className="ac-rows">
            {row.accounts.map((a) => {
              const stale = a.source === 'manual' && a.stale;
              const signIn = a.connection?.status === 'login_required';
              const sub = signIn
                ? 'Sign in again to update'
                : stale
                  ? a.balance_age_days == null
                    ? 'Update balance · no balance saved yet'
                    : `Update balance · ${plural(a.balance_age_days, 'day')} old`
                  : [a.interest_rate !== null ? `${formatRate(a.interest_rate)} interest` : null, rowSubline({ ...a, institution_name: null, mask: null }, now)].filter(Boolean).join(' · ');
              return (
                <li key={a.id}>
                  <Link to={`/accounts/${a.id}`} className="ac-row ac-row-kid" data-acct={a.id}>
                    <span className="ac-row-text">
                      <span className="ac-row-name">{a.name}</span>
                      <span className={`ac-row-sub${stale ? ' is-amber' : signIn ? ' is-red' : ''}`}>{sub}</span>
                    </span>
                    <span className="ac-row-amt">
                      <span className="ac-row-bal">{shownBalance(a)}</span>
                      <BalanceWord a={a} />
                    </span>
                    <span className="ac-chev" aria-hidden="true">
                      ›
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          <RenameGroup row={row} />
        </div>
      )}
    </div>
  );
}

/** The server's limit for a loan group's name. */
const GROUP_NAME_MAX = 40;

/**
 * "Rename this group": sets the loan group on every loan in it (empty = back to the automatic name).
 * One request per loan: a failed one is tried once more; if it still fails, the user is told plainly how
 * many got the new name, and Save name again finishes the rest (the loans already renamed moved
 * to the new group, so only the others are left in this one).
 */
function RenameGroup({ row }: { row: Extract<ListRow, { type: 'loans' }> }) {
  const { replace, say } = useAccounts();
  const { invalidate } = useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.title);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputId = useId();

  async function save(e: FormEvent) {
    e.preventDefault();
    const name = draft.trim();
    if (name === row.title) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setErr(null);
    const done: Account[] = [];
    let failed: unknown = null;
    for (const a of row.accounts) {
      try {
        done.push(await api.accounts.update(a.id, { loan_group: name || null }));
      } catch (e1) {
        if (e1 instanceof ApiError && e1.status === 401) {
          failed = e1;
          break;
        }
        try {
          done.push(await api.accounts.update(a.id, { loan_group: name || null })); // once more
        } catch (e2) {
          failed = e2;
          break;
        }
      }
    }
    // All at once, after the requests: renamed loans move to their new group together.
    for (const u of done) replace(u);
    if (done.length) invalidate();
    setBusy(false);
    if (failed === null) {
      setEditing(false);
      say(name ? `These loans are called “${name}” now.` : 'These loans use their automatic name again.');
      return;
    }
    if (failed instanceof ApiError && failed.status === 401) return;
    const total = row.accounts.length;
    const text = done.length
      ? `Only ${done.length} of ${total} loans got the new name. Press Save name to try the others again.`
      : `The name wasn’t saved. ${errorMessage(failed)}`;
    setErr(text);
    // This form may be gone (the loans left here can stop being a group): say it where the user will see it.
    if (done.length) say(text);
  }

  if (!editing) {
    return (
      <button
        type="button"
        className="ac-link-btn ac-rename"
        onClick={() => {
          setDraft(row.title);
          setEditing(true);
        }}
      >
        Rename this group
      </button>
    );
  }
  return (
    <form className="ac-rename-form" onSubmit={(e) => void save(e)}>
      <label htmlFor={inputId} className="ac-label">
        Group name
      </label>
      <div className="ac-inline">
        <input id={inputId} className="ac-input" value={draft} maxLength={GROUP_NAME_MAX} onChange={(e) => setDraft(e.target.value)} autoFocus disabled={busy} />
        <button type="submit" className="ac-btn ac-btn-outline-accent" disabled={busy}>
          {busy ? 'Saving…' : 'Save name'}
        </button>
        <button type="button" className="ac-btn ac-btn-quiet" onClick={() => setEditing(false)} disabled={busy}>
          Cancel
        </button>
      </div>
      <span className="ac-muted">Up to {GROUP_NAME_MAX} letters. Leave it empty to use the automatic name.</span>
      {err && (
        <p className="ac-error" role="alert">
          {err}
        </p>
      )}
    </form>
  );
}
