import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, ApiError, errorMessage, plaidKeysErrorCode, type PlaidKeysStatus, type PlaidKeysTest } from '../../api';
import { useApp } from '../../state';
import { useApi } from '../../lib/useApi';
import { formatDateTime, formatRelative, plural } from '../../lib/format';
import { Icon } from '../../components/Icon';
import { AddAccountModal, type AddAccountStart } from '../../components/AddAccountModal';
import { Modal } from '../../components/Modal';
import { Banner, PasswordField, Skeleton } from '../../components/ui';
import { useToast } from '../../components/Toast';
import { PlaidKeysForm } from './PlaidKeysForm';
import { BANK_SETUP, ENV_LONG, ENV_SHORT, PLAID_KEYS_URL, PLAID_SIGNUP_URL, keysRejected, maskedSecret, middleTruncate } from './plaidKeys';
import { KeepPrivateNote, PlaidLink } from './BankSetupParts';
import './bank-connection.css';

/**
 * Settings → Bank connection (Release 3.4): the user's own Plaid keys, stored
 * encrypted in the vault. States: not set up (guided 3 steps), connected
 * (saved in the vault), and read-only when the .env file provides the keys.
 * Lives on Manage banks (/settings/banks); deep links: /settings/banks?focus=bank-connection.
 */
export function BankConnectionSection() {
  const toast = useToast();
  const { refreshPlaid, invalidate } = useApp();
  const keys = useApi(() => api.plaidKeys.get(), []);
  const [editing, setEditing] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [addStart, setAddStart] = useState<AddAccountStart | null>(null);
  const s = keys.data;

  function saved(next: PlaidKeysStatus) {
    keys.setData(next);
    setEditing(false);
    setJustSaved(true);
    toast.push({
      tone: 'success',
      title: 'Connected to Plaid',
      body: next.linked_items === 0 ? 'Your keys are saved. Next, link your first bank.' : 'Your keys are saved and your banks keep syncing.',
    });
    void refreshPlaid();
    invalidate();
  }

  function removed() {
    setRemoveOpen(false);
    setEditing(false);
    setJustSaved(false);
    toast.push({ tone: 'success', title: 'Plaid keys removed', body: s?.source === 'env' ? undefined : 'Add keys again any time to link or sync banks.' });
    void refreshPlaid();
    invalidate();
  }

  function managedByEnv() {
    setEditing(false);
    toast.push({ tone: 'info', title: 'Keys are in the settings file', body: 'Iron Owl’s .env file has Plaid keys, so they’re managed there.' });
    keys.reload();
    void refreshPlaid();
  }

  let badge: ReactNode = null;
  if (s?.source === 'none') badge = <span className="badge">Not set up</span>;
  else if (s?.source === 'env') badge = <span className="badge badge-outline">Settings file (.env)</span>;
  else if (s?.source === 'vault' && s.env) {
    badge =
      keysRejected(s.last_test) ? (
        <span className="badge badge-warn">
          <Icon name="alert" />
          Check keys
        </span>
      ) : (
        <span className={`badge ${s.env === 'production' ? 'badge-ok' : 'badge-accent'}`}>
          <Icon name="check" />
          Connected · {ENV_SHORT[s.env]}
        </span>
      );
  }

  let body: ReactNode;
  if (keys.loading && !s) {
    body = (
      <div className="panel-body bc-body" role="status" aria-label="Loading bank connection">
        <Skeleton height={16} width="70%" />
        <Skeleton height={44} />
        <Skeleton height={44} />
      </div>
    );
  } else if (!s) {
    body = (
      <div className="panel-body bc-body">
        <p className="field-error">{errorMessage(keys.error)}</p>
        <div>
          <button type="button" className="btn btn-sm" onClick={keys.reload}>
            <Icon name="sync" />
            Try again
          </button>
        </div>
      </div>
    );
  } else if (s.source === 'env') {
    body = <EnvView status={s} onRemove={() => setRemoveOpen(true)} />;
  } else if (s.source === 'none') {
    body = <SetupView status={s} onSaved={saved} onManagedByEnv={managedByEnv} />;
  } else if (editing) {
    body = (
      <div className="panel-body bc-body">
        <div className="bc-step-body">
          <h3>Update your keys</h3>
          <p>Copy your keys from Plaid’s Keys page (Developers → Keys), then paste them below.</p>
          <p>
            <PlaidLink href={PLAID_KEYS_URL}>Open Plaid’s Keys page</PlaidLink>
          </p>
        </div>
        <PlaidKeysForm status={s} mode="update" onSaved={saved} onCancel={() => setEditing(false)} onManagedByEnv={managedByEnv} />
      </div>
    );
  } else {
    body = (
      <SavedView
        status={s}
        justSaved={justSaved}
        onLastTest={(t) => keys.setData((prev) => ({ ...(prev ?? s), last_test: t }))}
        onEdit={() => {
          setJustSaved(false);
          setEditing(true);
        }}
        onRemove={() => setRemoveOpen(true)}
        onLink={() => setAddStart({ step: 'link', kind: 'bank' })}
        onGone={keys.reload}
      />
    );
  }

  return (
    <section className="panel bc" id="bank-connection" aria-labelledby="bank-h">
      <div className="panel-head">
        <div>
          <h2 id="bank-h" tabIndex={-1}>
            Bank connection
          </h2>
          <p className="small muted">Iron Owl connects to your banks through Plaid, using keys from your own Plaid account.</p>
        </div>
        {badge}
      </div>

      {body}

      {s && (
        <RemoveKeysModal
          open={removeOpen}
          status={s}
          onClose={() => setRemoveOpen(false)}
          onRemoved={removed}
        />
      )}

      <AddAccountModal
        open={addStart !== null}
        start={addStart ?? undefined}
        onClose={() => setAddStart(null)}
        onDone={() => {
          setJustSaved(false);
          invalidate();
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------- not set up

function SetupView({ status, onSaved, onManagedByEnv }: { status: PlaidKeysStatus; onSaved: (s: PlaidKeysStatus) => void; onManagedByEnv: () => void }) {
  return (
    <div className="panel-body bc-body">
      {status.env_file_partial && (
        <Banner>
          Iron Owl’s settings file (.env) has only part of your Plaid keys, so Iron Owl is ignoring it. Put both PLAID_CLIENT_ID and PLAID_SECRET there, or
          leave both empty and use the steps below.
        </Banner>
      )}
      <p className="bc-lead">To link your banks, Iron Owl needs two keys from your own Plaid account. {BANK_SETUP.time}</p>
      <ol className="bc-steps">
        <li className="bc-step">
          <span className="bc-step-n" aria-hidden="true">
            1
          </span>
          <div className="bc-step-body">
            <h3>{BANK_SETUP.account.title}</h3>
            <p>{BANK_SETUP.account.body}</p>
            <p>
              <PlaidLink href={PLAID_SIGNUP_URL}>{BANK_SETUP.account.link}</PlaidLink>
            </p>
          </div>
        </li>
        <li className="bc-step">
          <span className="bc-step-n" aria-hidden="true">
            2
          </span>
          <div className="bc-step-body">
            <h3>{BANK_SETUP.keys.title}</h3>
            <p>{BANK_SETUP.keys.body}</p>
            <dl className="bc-keys">
              <dt>{BANK_SETUP.keys.clientId.name}</dt>
              <dd>{BANK_SETUP.keys.clientId.desc}</dd>
              <dt>{BANK_SETUP.keys.secret.name}</dt>
              <dd>{BANK_SETUP.keys.secret.desc}</dd>
            </dl>
            <KeepPrivateNote />
            <p>
              <PlaidLink href={PLAID_KEYS_URL}>{BANK_SETUP.keys.link}</PlaidLink>
            </p>
          </div>
        </li>
        <li className="bc-step">
          <span className="bc-step-n" aria-hidden="true">
            3
          </span>
          <div className="bc-step-body">
            <h3>{BANK_SETUP.paste.title}</h3>
            <p>
              {BANK_SETUP.paste.body} {BANK_SETUP.paste.password}
            </p>
            {status.linked_items > 0 && (
              <p className="acct-note bc-note">
                <Icon name="info" />
                <span>
                  You still have {plural(status.linked_items, 'linked bank')} from before. Paste the same keys {status.linked_items === 1 ? 'it was' : 'they were'}{' '}
                  linked with so {status.linked_items === 1 ? 'it keeps' : 'they keep'} syncing.
                </span>
              </p>
            )}
            <PlaidKeysForm status={status} mode="setup" onSaved={onSaved} onManagedByEnv={onManagedByEnv} />
          </div>
        </li>
      </ol>
      <p className="acct-note bc-note">
        <Icon name="lock" />
        <span>Your keys are saved encrypted with your Iron Owl password and never leave this computer except to talk to Plaid.</span>
      </p>
    </div>
  );
}

// ---------------------------------------------------------------- connected (saved in the vault)

function SavedView({
  status,
  justSaved,
  onLastTest,
  onEdit,
  onRemove,
  onLink,
  onGone,
}: {
  status: PlaidKeysStatus;
  justSaved: boolean;
  onLastTest: (t: PlaidKeysTest) => void;
  onEdit: () => void;
  onRemove: () => void;
  onLink: () => void;
  /** The keys vanished server-side (409 not_set): reload. */
  onGone: () => void;
}) {
  const env = status.env ?? 'sandbox';
  const check = useActiveCheck({ onResult: onLastTest, onGone });
  const last = status.last_test;
  const failed = !!last && !last.ok;
  // Plaid said no (fix the keys) vs. couldn't check (try again later; the keys may be fine).
  const rejected = keysRejected(last);
  const linkRef = useRef<HTMLButtonElement>(null);
  const headRef = useRef<HTMLParagraphElement>(null);

  // Right after saving, put focus on the next step.
  useEffect(() => {
    if (justSaved) (linkRef.current ?? headRef.current)?.focus();
  }, [justSaved]);

  return (
    <div className="panel-body bc-body">
      <div className={`bc-status ${failed ? 'is-bad' : 'is-ok'}`} aria-live="polite">
        <Icon name={failed ? 'alert' : 'check'} />
        <div>
          <p className="bc-status-title" ref={headRef} tabIndex={-1}>
            {rejected ? 'Plaid didn’t accept your keys last time' : failed ? 'Couldn’t check your keys last time' : `Connected to Plaid — ${ENV_SHORT[env]}`}
          </p>
          <p className="bc-status-sub">
            {check.checking ? (
              <>Checking with Plaid…</>
            ) : check.error ? (
              check.error
            ) : last ? (
              last.ok ? (
                `Last checked: worked, ${formatRelative(last.at)}.`
              ) : (
                <>Last checked: didn’t work — {last.message}</>
              )
            ) : (
              'Not checked yet.'
            )}
          </p>
          {failed && last?.plaid_code && (
            <details className="bc-details">
              <summary>Details</summary>
              <span className="small muted">
                Error code: <code>{last.plaid_code}</code>
              </span>
            </details>
          )}
        </div>
        {rejected && (
          <button type="button" className="btn btn-primary bc-status-action" onClick={onEdit}>
            <Icon name="key" />
            Fix keys
          </button>
        )}
      </div>

      {status.linked_items === 0 && !rejected && (
        <div className="bc-next">
          <div>
            <h3>Next: link your first bank</h3>
            <p>Plaid opens a secure window where you pick your bank and sign in to it. Iron Owl never sees your bank password.</p>
          </div>
          <button ref={linkRef} type="button" className="btn btn-primary btn-lg" onClick={onLink}>
            <Icon name="bank" />
            Link your first bank
          </button>
        </div>
      )}

      <dl className="kv bc-kv">
        <dt>Client ID</dt>
        <dd>
          <code title={status.client_id ?? undefined}>{middleTruncate(status.client_id)}</code>
        </dd>
        <dt>Secret</dt>
        <dd>
          <code>{maskedSecret(status.secret_hint)}</code>
        </dd>
        <dt>Banks</dt>
        <dd>{ENV_LONG[env]}</dd>
        {status.saved_at && (
          <>
            <dt>Saved</dt>
            <dd>{formatDateTime(status.saved_at)}</dd>
          </>
        )}
      </dl>

      {env === 'sandbox' && (
        <p className="acct-note bc-note">
          <Icon name="info" />
          <span>
            These are test keys — they only work with Plaid’s pretend banks. When Plaid approves you for real banks, come back, choose Update keys and paste
            the Production secret from Plaid’s Keys page. <PlaidLink href={PLAID_KEYS_URL}>Open Plaid’s Keys page</PlaidLink>
          </span>
        </p>
      )}

      <div className="bc-actions">
        <button type="button" className="btn" onClick={() => void check.run()} disabled={check.checking}>
          <Icon name="sync" className={check.checking ? 'spin' : undefined} />
          {check.checking ? 'Checking…' : 'Check connection'}
        </button>
        <button type="button" className="btn" onClick={onEdit}>
          <Icon name="key" />
          Update keys
        </button>
        <button type="button" className="btn btn-ghost btn-danger-quiet" onClick={onRemove}>
          <Icon name="trash" />
          Remove keys
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- keys from the .env file (owner's setup)

function EnvView({ status, onRemove }: { status: PlaidKeysStatus; onRemove: () => void }) {
  const [result, setResult] = useState<PlaidKeysTest | null>(null);
  const check = useActiveCheck({ onResult: setResult });
  const env = status.env ?? 'sandbox';
  return (
    <div className="panel-body bc-body">
      <p className="small">
        Using keys from Iron Owl’s settings file (.env). They’re managed there, not here. To manage them here instead, delete <code>PLAID_CLIENT_ID</code> and{' '}
        <code>PLAID_SECRET</code> from the .env file and restart Iron Owl.
      </p>
      <dl className="kv bc-kv">
        <dt>Client ID</dt>
        <dd>
          <code title={status.client_id ?? undefined}>{middleTruncate(status.client_id)}</code>
        </dd>
        <dt>Secret</dt>
        <dd>
          <code>{maskedSecret(status.secret_hint)}</code>
        </dd>
        <dt>Banks</dt>
        <dd>{ENV_LONG[env]}</dd>
      </dl>
      <div className="bc-actions">
        <button type="button" className="btn" onClick={() => void check.run()} disabled={check.checking}>
          <Icon name="sync" className={check.checking ? 'spin' : undefined} />
          {check.checking ? 'Checking…' : 'Check connection'}
        </button>
      </div>
      <div className={`bc-check ${check.error || (result && !result.ok) ? 'is-bad' : result ? 'is-ok' : ''}`} aria-live="polite">
        {check.checking ? null : check.error ? (
          <>
            <Icon name="alert" />
            <span>{check.error}</span>
          </>
        ) : result ? (
          <>
            <Icon name={result.ok ? 'check' : 'alert'} />
            <span>
              {result.message}
              {!result.ok && result.plaid_code && (
                <details className="bc-details">
                  <summary>Details</summary>
                  <span className="small muted">
                    Error code: <code>{result.plaid_code}</code>
                  </span>
                </details>
              )}
            </span>
          </>
        ) : null}
      </div>
      {status.vault_keys_ignored && (
        <div className="bc-inline-note is-info">
          <Icon name="info" />
          <span>Keys saved in Iron Owl are ignored while the .env file has keys.</span>
          <button type="button" className="btn btn-sm btn-ghost btn-danger-quiet" onClick={onRemove}>
            <Icon name="trash" />
            Remove saved keys
          </button>
        </div>
      )}
    </div>
  );
}

/** "Check connection": tests the active keys (POST /test with no body). */
function useActiveCheck({ onResult, onGone }: { onResult: (t: PlaidKeysTest) => void; onGone?: () => void }) {
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  async function run() {
    if (checking) return;
    setChecking(true);
    setError(null);
    try {
      const t = await api.plaidKeys.test();
      if (live.current) onResult(t);
    } catch (e) {
      if (!live.current) return;
      if (e instanceof ApiError && e.status === 429) setError('Plaid checks are busy right now. Wait a few seconds, then try again.');
      else if (plaidKeysErrorCode(e) === 'not_set') onGone?.();
      else if (!(e instanceof ApiError && e.status === 401)) setError(errorMessage(e));
    } finally {
      if (live.current) setChecking(false);
    }
  }
  return { checking, error, run };
}

// ---------------------------------------------------------------- remove (destructive: keeps a confirmation modal)

function RemoveKeysModal({
  open,
  status,
  onClose,
  onRemoved,
}: {
  open: boolean;
  status: PlaidKeysStatus;
  onClose: () => void;
  onRemoved: () => void;
}) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pwRef = useRef<HTMLInputElement>(null);
  const formId = 'bc-remove-form';
  const ignored = status.source === 'env';

  useEffect(() => {
    if (!open) return;
    const h = requestAnimationFrame(() => pwRef.current?.focus());
    return () => cancelAnimationFrame(h);
  }, [open]);

  function close() {
    if (busy) return;
    setPassword('');
    setError(null);
    onClose();
  }

  async function remove(e: FormEvent) {
    e.preventDefault();
    if (!password) return setError('Enter your Iron Owl password.');
    setBusy(true);
    setError(null);
    try {
      await api.plaidKeys.remove(password);
      setPassword('');
      onRemoved();
    } catch (err) {
      setPassword('');
      if (err instanceof ApiError && err.status === 401) setError('That’s not your Iron Owl password.');
      else if (err instanceof ApiError && err.status === 429) setError(`Too many attempts. Try again in ${err.retryAfter ?? 30} seconds.`);
      else setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      width={440}
      title={ignored ? 'Remove saved keys?' : 'Remove Plaid keys?'}
      subtitle={ignored ? 'They aren’t being used while the .env file has keys.' : 'Iron Owl won’t be able to link or sync banks until you add keys again.'}
      onClose={close}
      busy={busy}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button type="submit" form={formId} className="btn btn-danger" disabled={busy}>
            <Icon name="trash" />
            {busy ? 'Removing…' : 'Remove keys'}
          </button>
        </>
      }
    >
      <form id={formId} onSubmit={remove} noValidate className="modal-form bc-remove-form">
        {!ignored && status.linked_items > 0 && (
          <p>
            Your {plural(status.linked_items, 'linked bank')} stay{status.linked_items === 1 ? 's' : ''} in Iron Owl but stop syncing. To disconnect{' '}
            {status.linked_items === 1 ? 'it' : 'them'} at Plaid, remove {status.linked_items === 1 ? 'it' : 'them'} under Linked institutions before removing
            the keys.
          </p>
        )}
        <PasswordField
          label="Your Iron Owl password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            setError(null);
          }}
          inputRef={pwRef}
          disabled={busy}
          error={error}
        />
      </form>
    </Modal>
  );
}
