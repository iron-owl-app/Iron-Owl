import { useEffect, useId, useState, type FormEvent, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, ApiError, errorMessage, type Account, type AccountPatch } from '../../api';
import { useApp, useUpdated } from '../../state';
import { formatDate, formatMoney, formatRate, initialsFor } from '../../lib/format';
import { Skeleton } from '../../components/ui';
import { ErrorPanel } from '../../components/ErrorPanel';
import { useBankReauth } from '../../components/useBankReauth';
import { useAccounts } from './AccountsShell';
import { hueStyle, shownBalance } from './AccountsList';
import { balanceDraft, balanceHeading, GROUP_OF, groupMeta, isDebt, isOverdrawn, parseAmount, typeLabel, updatedWords } from './accountGroups';
import { remindDays } from '../../components/manualAccount';

const longDay = new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const fail = (e: unknown) => (e instanceof ApiError && e.status === 401 ? null : errorMessage(e));

/**
 * One account (layout A: it takes the list's place): the balance and what needs doing, its
 * details, then what the user can change. Bank-connected accounts can be renamed and hidden (never
 * disconnected from here: Plaid connections are limited); ones the user adds themselves also take a new
 * balance (with an exact Undo) and can be removed (with Undo).
 */
export function AccountDetail() {
  const { id } = useParams();
  const { accounts, error, reload } = useAccounts();
  const account = accounts?.find((a) => String(a.id) === id);

  if (!accounts) {
    if (error) return <ErrorPanel error={error} onRetry={reload} title="Couldn’t load this account" />;
    return (
      <div className="ac-detail" role="status" aria-label="Loading the account">
        <BackLink />
        <Skeleton height={260} style={{ borderRadius: 24, display: 'block' }} />
      </div>
    );
  }
  if (!account) {
    return (
      <div className="ac-detail">
        <BackLink />
        <section className="ac-card ac-pad">
          <h2 className="ac-h3">This account isn’t in Iron Owl any more</h2>
          <p className="ac-muted-p">It may have been removed. Your other accounts are on the list.</p>
        </section>
      </div>
    );
  }
  // Keyed by id: moving to another account starts its forms fresh.
  return <Detail key={account.id} a={account} />;
}

function BackLink({ from }: { from?: number }) {
  return (
    <Link to="/accounts" state={from !== undefined ? { from } : undefined} className="ac-back">
      <span aria-hidden="true">← </span>All accounts
    </Link>
  );
}

function Detail({ a }: { a: Account }) {
  const meta = groupMeta(a.category);
  const manual = a.source === 'manual';
  const investment = GROUP_OF[a.category] === 'investments';
  return (
    <div className="ac-detail" style={hueStyle(meta.hue)}>
      <BackLink from={a.id} />
      <div className="ac-detail-cols">
        <div className="ac-detail-main">
          <Summary a={a} />
          <Details a={a} />
          {investment && (
            <Link to="/investments" className="ac-card ac-pad ac-holds">
              See what it holds<span aria-hidden="true"> →</span>
            </Link>
          )}
        </div>
        <div className="ac-detail-side">
          {manual && <UpdateBalance a={a} />}
          <NameCard a={a} />
          {isDebt(a) && <PaymentDetails a={a} />}
          {manual ? <RemoveCard a={a} /> : <HideCard a={a} />}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- summary

function Summary({ a }: { a: Account }) {
  const { now } = useUpdated();
  const reauth = useBankReauth();
  const c = a.connection;
  const bank = a.institution_name ?? 'Your bank';
  const bankCalls = a.source === 'plaid' && a.bank_name && a.bank_name.trim() !== a.name.trim() ? a.bank_name : null;
  const avail = a.available_balance;
  const showAvail = GROUP_OF[a.category] === 'cash' && avail !== null && Math.abs(avail - a.current_balance) >= 0.005;
  const since = c?.last_synced_at ? longDay.format(new Date(c.last_synced_at)) : null;
  const stale = a.source === 'manual' && !!a.stale;
  const overdrawn = isOverdrawn(a);

  return (
    <section className="ac-card ac-pad ac-summary" aria-labelledby="ac-name">
      <div className="ac-summary-top">
        <span className="ac-avatar ac-avatar-lg" aria-hidden="true">
          {initialsFor(a.name)}
        </span>
        <div className="ac-min0">
          <div className="ac-kicker">
            {typeLabel(a)}
            {a.hidden ? ' · hidden' : ''}
          </div>
          <h2 id="ac-name" className="ac-detail-name">
            {a.name}
          </h2>
          {bankCalls && <div className="ac-muted">Your bank calls it “{bankCalls}”</div>}
        </div>
      </div>
      <div>
        <div className={`ac-bal-label${overdrawn ? ' is-amber' : ''}`}>{balanceHeading(a)}</div>
        {/* Overdrawn: "Overdrawn by $50.00" (the heading carries the sign's meaning; amber, never red). */}
        <div className={`ac-num ac-bal${overdrawn ? ' is-amber' : ''}`}>
          {overdrawn ? formatMoney(-a.current_balance, { currency: a.currency }) : shownBalance(a)}
        </div>
        {showAvail && <div className="ac-muted-p">{formatMoney(avail!, { currency: a.currency })} available to spend</div>}
        <div className="ac-muted-p">{cap(updatedWords(a, now))}</div>
      </div>

      {c?.status === 'login_required' && (
        <div className="ac-box is-red">
          <span className="ac-box-text">
            <strong>{bank} needs you to sign in again.</strong>
            {since ? ` The balance above is from ${since}.` : ' Until you do, the balance above may be out of date.'}
          </span>
          <button
            type="button"
            className="ac-btn ac-btn-light"
            onClick={() => reauth.start({ id: c.item_id, kind: c.kind, institution_name: a.institution_name })}
            aria-disabled={reauth.busyItemId !== null || undefined}
          >
            {reauth.phase === 'updating' ? 'Updating…' : reauth.phase === 'opening' ? 'Opening sign-in…' : 'Sign in again'}
          </button>
        </div>
      )}
      {c?.status === 'error' && (
        <div className="ac-box is-amber">
          <span className="ac-box-text">
            <strong>{bank} isn’t updating right now.</strong> Iron Owl will try again.
            {since ? ` The balance above is from ${since}.` : ''}
          </span>
        </div>
      )}
      {stale && (
        <div className="ac-box is-amber">
          <span className="ac-box-text">
            {a.balance_age_days == null ? (
              <>
                <strong>This account doesn’t have a balance yet.</strong> Type in the balance from your latest statement below.
              </>
            ) : (
              <>
                <strong>It’s been {a.balance_age_days} days since you updated this balance.</strong> Type in the balance from your latest statement below.
              </>
            )}
          </span>
        </div>
      )}
      {reauth.launcher}
    </section>
  );
}

function Details({ a }: { a: Account }) {
  const rows: [string, ReactNode][] = [];
  if (a.institution_name) rows.push(['Bank', a.institution_name]);
  if (a.mask) rows.push(['Account number', `Ends in ${a.mask}`]);
  rows.push(['Type', typeLabel(a)]);
  if (isDebt(a)) rows.push(['Interest rate', a.interest_rate !== null ? formatRate(a.interest_rate) : 'Not known yet']);
  if (a.category === 'credit') rows.push(['Card limit', a.credit_limit != null ? formatMoney(a.credit_limit) : 'Your bank didn’t share it']);
  rows.push(['How it updates', a.source === 'manual' ? 'You type in the balance' : 'Connected. Updates on its own.']);
  return (
    <section className="ac-card ac-pad" aria-labelledby="ac-details-h">
      <h3 id="ac-details-h" className="ac-h3">
        Account details
      </h3>
      <dl className="ac-facts">
        {rows.map(([k, v]) => (
          <div key={k} className="ac-fact">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

// ---------------------------------------------------------------- update balance (manual)

function UpdateBalance({ a }: { a: Account }) {
  const { replace, say } = useAccounts();
  const { invalidate } = useApp();
  const debt = isDebt(a);
  // Debts: the amount owed; anything else with its sign (an overdrawn account starts at "-50.00").
  const [draft, setDraft] = useState(() => balanceDraft(a));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputId = useId();
  const errId = useId();
  const value = parseAmount(draft);
  const bad = value === null || Number.isNaN(value) || (debt && value < 0);
  const stale = !!a.stale;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (bad || busy) {
      setErr(debt ? 'Type what you owe, like 1250.00' : 'Type the balance, like 1250.00, or -50.00 if it’s overdrawn');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const res = await api.accountsV2.setBalance(a.id, value!);
      replace(res.account);
      invalidate();
      setDraft(balanceDraft(res.account));
      say(`${res.account.name} balance saved: ${shownBalance(res.account)}.`, undefined, async () => {
        const back = await api.accountsV2.undoBalance(res.undo.token);
        replace(back);
        setDraft(balanceDraft(back));
        invalidate();
      });
    } catch (e2) {
      setErr(fail(e2));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={`ac-card ac-pad ac-form${stale ? ' is-amber' : ''}`} onSubmit={(e) => void save(e)} noValidate aria-labelledby={`${inputId}-h`}>
      <h3 id={`${inputId}-h`} className="ac-h3">
        Update balance
      </h3>
      <p className="ac-muted-p">
        You add this one yourself, so Iron Owl can’t update it.{stale ? '' : ` We’ll remind you after ${remindDays(a.category)} days.`}
      </p>
      <label htmlFor={inputId} className="ac-label">
        {debt ? 'How much you owe now' : 'Balance now'}
      </label>
      <span className="ac-money ac-money-narrow">
        <span className="ac-money-sign" aria-hidden="true">
          $
        </span>
        <input
          id={inputId}
          className="ac-input ac-input-money"
          inputMode="decimal"
          autoComplete="off"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value.replace(debt ? /[^0-9.,]/g : /[^0-9.,\-−]/g, ''));
            setErr(null);
          }}
          aria-invalid={!!err || undefined}
          aria-describedby={err ? errId : undefined}
          disabled={busy}
        />
      </span>
      {err && (
        <p className="ac-error" id={errId} role="alert">
          {err}
        </p>
      )}
      <button type="submit" className="ac-btn ac-btn-primary ac-self-start" disabled={bad || busy}>
        {busy ? 'Saving…' : 'Save balance'}
      </button>
    </form>
  );
}

// ---------------------------------------------------------------- nickname (connected) / name (manual)

function NameCard({ a }: { a: Account }) {
  const { replace, say } = useAccounts();
  const { invalidate } = useApp();
  const plaid = a.source === 'plaid';
  const [draft, setDraft] = useState(a.name);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputId = useId();
  const trimmed = draft.trim();
  const same = trimmed === a.name.trim();
  const bankName = plaid ? (a.bank_name?.trim() ?? '') : '';

  async function rename(name: string) {
    const old = a.name;
    setBusy(true);
    setErr(null);
    try {
      const u = await api.accounts.update(a.id, { name });
      replace(u);
      setDraft(u.name);
      invalidate();
      const text = plaid && name === bankName ? 'Using the bank’s name again.' : `It’s called “${u.name}” now.`;
      say(text, undefined, async () => {
        const back = await api.accounts.update(a.id, { name: old });
        replace(back);
        setDraft(back.name);
        invalidate();
      });
    } catch (e) {
      setErr(fail(e));
    } finally {
      setBusy(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!trimmed) {
      setErr(plaid ? 'Type a nickname, or use the bank’s name.' : 'Type a name for this account.');
      return;
    }
    if (same || busy) return;
    void rename(trimmed);
  }

  return (
    <form className="ac-card ac-pad ac-form" onSubmit={submit} noValidate aria-labelledby={`${inputId}-h`}>
      <h3 id={`${inputId}-h`} className="ac-h3">
        {plaid ? 'Nickname' : 'Name'}
      </h3>
      <p className="ac-muted-p">A name that’s easy to recognize. It shows everywhere in Iron Owl.</p>
      <label htmlFor={inputId} className="sr-only">
        {plaid ? 'Nickname' : 'Name'}
      </label>
      <input
        id={inputId}
        className="ac-input"
        value={draft}
        maxLength={100}
        placeholder={bankName || undefined}
        onChange={(e) => {
          setDraft(e.target.value);
          setErr(null);
        }}
        disabled={busy}
        aria-invalid={!!err || undefined}
      />
      {err && (
        <p className="ac-error" role="alert">
          {err}
        </p>
      )}
      <div className="ac-actions">
        <button type="submit" className="ac-btn ac-btn-outline-accent" disabled={same || busy}>
          {busy ? 'Saving…' : plaid ? 'Save nickname' : 'Save name'}
        </button>
        {plaid && bankName && bankName !== a.name.trim() && (
          <button type="button" className="ac-btn ac-btn-quiet" onClick={() => void rename(bankName)} disabled={busy}>
            Use the bank’s name
          </button>
        )}
      </div>
    </form>
  );
}

// ---------------------------------------------------------------- payment details (debts)

type TermKey = 'minimum_payment' | 'interest_rate' | 'next_payment_due';

function PaymentDetails({ a }: { a: Account }) {
  const { replace, say } = useAccounts();
  const { invalidate } = useApp();
  const plaid = a.source === 'plaid';
  const bank = a.institution_name ?? 'your bank';
  const initial = () => ({
    minimum_payment: a.minimum_payment !== null ? a.minimum_payment.toFixed(2) : '',
    interest_rate: a.interest_rate !== null ? String(a.interest_rate) : '',
    next_payment_due: a.next_payment_due ?? '',
  });
  const [draft, setDraft] = useState(initial);
  // Connected accounts: a value the bank may have sent stays put unless the user chooses to change it.
  const [unlocked, setUnlocked] = useState<Record<TermKey, boolean>>({ minimum_payment: false, interest_rate: false, next_payment_due: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const uid = useId();
  useEffect(() => setDraft(initial()), [a.minimum_payment, a.interest_rate, a.next_payment_due]); // eslint-disable-line react-hooks/exhaustive-deps

  const editable = (k: TermKey) => !plaid || a[k] === null || unlocked[k];
  const current: Record<TermKey, string> = initial();
  const changed = (Object.keys(draft) as TermKey[]).filter((k) => editable(k) && draft[k].trim() !== current[k]);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!changed.length || busy) return;
    const patch: AccountPatch = {};
    const before: AccountPatch = {};
    for (const k of changed) {
      const raw = draft[k].trim();
      if (k === 'minimum_payment') {
        const v = raw ? parseAmount(raw) : null;
        if (v !== null && (Number.isNaN(v) || v < 0)) return setErr('Type the monthly payment like 95.00');
        patch.minimum_payment = v;
        before.minimum_payment = a.minimum_payment;
      } else if (k === 'interest_rate') {
        const v = raw ? Number(raw.replace(/[%\s]/g, '')) : null;
        if (v !== null && (!Number.isFinite(v) || v < 0 || v > 100)) return setErr('Type the interest rate as a number from 0 to 100, like 6.5');
        patch.interest_rate = v;
        before.interest_rate = a.interest_rate;
      } else {
        if (raw && !/^\d{4}-\d{2}-\d{2}$/.test(raw)) return setErr('Pick the date the next payment is due.');
        patch.next_payment_due = raw || null;
        before.next_payment_due = a.next_payment_due;
      }
    }
    setBusy(true);
    setErr(null);
    try {
      const u = await api.accounts.update(a.id, patch);
      replace(u);
      invalidate();
      setUnlocked({ minimum_payment: false, interest_rate: false, next_payment_due: false });
      say('Payment details saved.', undefined, async () => {
        replace(await api.accounts.update(a.id, before));
        invalidate();
      });
    } catch (e2) {
      setErr(fail(e2));
    } finally {
      setBusy(false);
    }
  }

  const field = (k: TermKey, label: string, input: ReactNode, shown: string) => (
    <div className="ac-field">
      <label htmlFor={`${uid}-${k}`} className="ac-label">
        {label}
      </label>
      {editable(k) ? (
        input
      ) : (
        <div className="ac-locked">
          <span className="ac-locked-val">{shown}</span>
          <button type="button" className="ac-link-btn" onClick={() => setUnlocked((u) => ({ ...u, [k]: true }))} aria-label={`Change ${label.toLowerCase()}`}>
            Change
          </button>
        </div>
      )}
    </div>
  );
  const set = (k: TermKey) => (e: { target: { value: string } }) => {
    setDraft((d) => ({ ...d, [k]: e.target.value }));
    setErr(null);
  };
  const anyUnlockedBankValue = plaid && (Object.keys(unlocked) as TermKey[]).some((k) => unlocked[k] && a[k] !== null);

  return (
    <form className="ac-card ac-pad ac-form" onSubmit={(e) => void save(e)} noValidate aria-labelledby={`${uid}-h`}>
      <h3 id={`${uid}-h`} className="ac-h3">
        Payment details
      </h3>
      <p className="ac-muted-p">
        {!plaid
          ? 'Paying off debt uses these to plan.'
          : a.minimum_payment === null || a.interest_rate === null || a.next_payment_due === null
            ? `Fill in what ${bank} didn’t send. Paying off debt uses these to plan.`
            : 'Paying off debt uses these to plan.'}
      </p>
      <div className="ac-grid2">
        {field(
          'minimum_payment',
          'Monthly payment',
          <span className="ac-money">
            <span className="ac-money-sign" aria-hidden="true">
              $
            </span>
            <input id={`${uid}-minimum_payment`} className="ac-input ac-input-money" inputMode="decimal" value={draft.minimum_payment} onChange={set('minimum_payment')} disabled={busy} />
          </span>,
          a.minimum_payment !== null ? formatMoney(a.minimum_payment) : '',
        )}
        {field(
          'interest_rate',
          'Interest rate',
          <span className="ac-money">
            <input id={`${uid}-interest_rate`} className="ac-input ac-input-pct" inputMode="decimal" value={draft.interest_rate} onChange={set('interest_rate')} disabled={busy} />
            <span className="ac-money-pct" aria-hidden="true">
              %
            </span>
          </span>,
          a.interest_rate !== null ? formatRate(a.interest_rate) : '',
        )}
        {field(
          'next_payment_due',
          'Next payment due',
          <input id={`${uid}-next_payment_due`} type="date" className="ac-input" value={draft.next_payment_due} onChange={set('next_payment_due')} disabled={busy} />,
          a.next_payment_due ? formatDate(a.next_payment_due) : '',
        )}
      </div>
      {anyUnlockedBankValue && <p className="ac-muted-p">If {bank} sends this, its next update puts its own number back.</p>}
      {err && (
        <p className="ac-error" role="alert">
          {err}
        </p>
      )}
      {(!plaid || (Object.keys(draft) as TermKey[]).some(editable)) && (
        <button type="submit" className="ac-btn ac-btn-outline-accent ac-self-start" disabled={!changed.length || busy}>
          {busy ? 'Saving…' : 'Save payment details'}
        </button>
      )}
    </form>
  );
}

// ---------------------------------------------------------------- hide (connected) / remove (manual)

function HideCard({ a }: { a: Account }) {
  const { replace, say, setHiddenOpen } = useAccounts();
  const { invalidate } = useApp();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const bank = a.institution_name ?? 'this bank';

  async function toggle() {
    const hide = !a.hidden;
    setBusy(true);
    setErr(null);
    try {
      replace(await api.accounts.update(a.id, { hidden: hide }));
      invalidate();
      if (hide) setHiddenOpen(true);
      say(hide ? `${a.name} is hidden.` : `${a.name} is back in your accounts.`, undefined, async () => {
        replace(await api.accounts.update(a.id, { hidden: !hide }));
        invalidate();
      });
    } catch (e) {
      setErr(fail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="ac-card ac-pad ac-form" aria-labelledby="ac-hide-h">
      <h3 id="ac-hide-h" className="ac-h3">
        {a.hidden ? 'This account is hidden' : 'Hide this account'}
      </h3>
      <button type="button" className="ac-btn ac-self-start" onClick={() => void toggle()} disabled={busy}>
        {a.hidden ? 'Show it again' : 'Hide this account'}
      </button>
      <p className="ac-muted-p">
        {a.hidden ? 'It will count in your totals again.' : 'It won’t count in any totals. You can show it again from “Hidden accounts”.'}
      </p>
      {err && (
        <p className="ac-error" role="alert">
          {err}
        </p>
      )}
      <p className="ac-muted-p ac-divided">
        To stop connecting to {bank}, go to <Link to="/settings/banks">Settings › Banks</Link>.
      </p>
    </section>
  );
}

function RemoveCard({ a }: { a: Account }) {
  const { drop, add, replace, say } = useAccounts();
  const { invalidate } = useApp();
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function remove() {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.accountsV2.remove(a.id);
      drop(a.id);
      invalidate();
      navigate('/accounts');
      say(`${res.undo.name || a.name} was removed.`, undefined, async () => {
        add(await api.accountsV2.restore(res.undo.token));
        invalidate();
      });
    } catch (e) {
      setErr(fail(e));
      setBusy(false);
    }
  }

  async function unhide() {
    setBusy(true);
    try {
      replace(await api.accounts.update(a.id, { hidden: false }));
      invalidate();
      say(`${a.name} is back in your accounts.`);
    } catch (e) {
      setErr(fail(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="ac-card ac-pad ac-form" aria-labelledby="ac-remove-h">
      <h3 id="ac-remove-h" className="ac-h3">
        {a.hidden ? 'Show or remove' : 'Remove this account'}
      </h3>
      {a.hidden && (
        <>
          {/* Hidden before 3.10 (manual accounts are removed now, not hidden): let the user bring it back. */}
          <button type="button" className="ac-btn ac-self-start" onClick={() => void unhide()} disabled={busy}>
            Show it again
          </button>
          <p className="ac-muted-p ac-divided-after">It will count in your totals again.</p>
        </>
      )}
      {confirming ? (
        <div className="ac-confirm" role="group" aria-labelledby="ac-remove-q">
          <p id="ac-remove-q" className="ac-confirm-q">
            <strong>Remove {a.name}?</strong> Its balance history is removed too.
          </p>
          <div className="ac-actions">
            <button type="button" className="ac-btn ac-btn-light" onClick={() => void remove()} disabled={busy} autoFocus>
              {busy ? 'Removing…' : 'Yes, remove it'}
            </button>
            <button type="button" className="ac-btn" onClick={() => setConfirming(false)} disabled={busy}>
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <>
          <button type="button" className="ac-btn ac-self-start" onClick={() => setConfirming(true)}>
            Remove from Iron Owl
          </button>
          <p className="ac-muted-p">For an account you closed or paid off. You can undo it right after.</p>
        </>
      )}
      {err && (
        <p className="ac-error" role="alert">
          {err}
        </p>
      )}
    </section>
  );
}
