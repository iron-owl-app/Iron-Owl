import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import {
  api,
  ApiError,
  errorMessage,
  plaidKeysErrorCode,
  plaidKeysErrorPlaidCode,
  type PlaidEnv,
  type PlaidKeysStatus,
  type PlaidKeysTest,
  type PlaidKeysTestInput,
} from '../../api';
import { plural } from '../../lib/format';
import { Icon } from '../../components/Icon';
import { PasswordField } from '../../components/ui';
import { CLIENT_ID_LEN, ENV_LONG, KEY_RE, SECRET_LEN, cleanKey, keysRejected } from './plaidKeys';

/** Quiet time after the last edit before the keys are checked automatically. */
const DEBOUNCE_MS = 600;
const SLOW_DEBOUNCE_MS = 2500;
/** The server allows one check every 2s; wait that long between starts so we rarely see a 429. */
const MIN_SPACING_MS = 2100;

/** Result of the latest automatic check, tagged with the edit it belongs to (`v`) so stale ones are dropped. */
type Check =
  | { v: number; state: 'checking' }
  | { v: number; state: 'done'; result: PlaidKeysTest }
  | { v: number; state: 'error'; message: string };

/**
 * Paste keys → they're checked with Plaid automatically → confirm your
 * password → Save and connect. Used for first-time setup and "Update keys".
 *
 * Secret hygiene: the secret lives only in this component's state and the
 * `latest` ref mirroring it. It's cleared after a save and dropped on unmount
 * (Cancel, lock, navigation). Never logged, stored, or put in a URL.
 */
export function PlaidKeysForm({
  status,
  mode,
  onSaved,
  onCancel,
  onManagedByEnv,
  intro,
}: {
  status: PlaidKeysStatus;
  mode: 'setup' | 'update';
  onSaved: (s: PlaidKeysStatus) => void;
  onCancel?: () => void;
  /** PUT said the .env file owns the keys now: reload the card. */
  onManagedByEnv: () => void;
  /** Rendered between the fields and the check result (setup's numbered step label, etc.). */
  intro?: ReactNode;
}) {
  const uid = useId();
  // Linked banks belong to one Plaid account and environment, so only the secret can change.
  const locked = mode === 'update' && status.source === 'vault' && status.linked_items > 0 && !!status.client_id && !!status.env;
  const [clientId, setClientId] = useState(mode === 'update' ? (status.client_id ?? '') : '');
  const [secret, setSecret] = useState('');
  const [manualEnv, setManualEnv] = useState<PlaidEnv | null>(null);
  const [touched, setTouched] = useState({ client: false, secret: false });
  const [version, setVersion] = useState(0);
  const [check, setCheck] = useState<Check | null>(null);
  const [password, setPassword] = useState('');
  const [pwError, setPwError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const pwRef = useRef<HTMLInputElement>(null);
  const secretRef = useRef<HTMLInputElement>(null);

  // "Update keys": the secret is what usually changes, so start there.
  useEffect(() => {
    if (mode === 'update') secretRef.current?.focus();
  }, [mode]);

  const swapped = !locked && clientId.length === SECRET_LEN && secret.length === CLIENT_ID_LEN && KEY_RE.test(clientId) && KEY_RE.test(secret);
  const same = clientId !== '' && clientId === secret;
  const env: PlaidEnv | 'auto' = locked ? status.env! : (manualEnv ?? 'auto');
  const input: PlaidKeysTestInput | null =
    KEY_RE.test(clientId) && KEY_RE.test(secret) && !swapped && !same ? { client_id: clientId, secret, env } : null;

  // ------------------------------------------------------------ automatic check
  // One request in flight; edits made meanwhile queue a single follow-up with the newest values.

  const latest = useRef({ input, version });
  latest.current = { input, version };
  const inFlight = useRef(false);
  const queued = useRef(false);
  const lastStart = useRef(0);
  const timer = useRef<number | undefined>(undefined);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      window.clearTimeout(timer.current);
    };
  }, []);

  const schedule = useCallback((fn: () => void, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(fn, ms);
  }, []);

  const run = useCallback(
    async (isRetry = false): Promise<void> => {
      const { input: keys, version: v } = latest.current;
      if (!keys || !mounted.current) return;
      if (inFlight.current) {
        queued.current = true;
        return;
      }
      const wait = lastStart.current + MIN_SPACING_MS - Date.now();
      if (wait > 0) {
        setCheck({ v, state: 'checking' });
        schedule(() => void run(isRetry), wait);
        return;
      }
      inFlight.current = true;
      queued.current = false;
      lastStart.current = Date.now();
      setCheck({ v, state: 'checking' });
      try {
        const result = await api.plaidKeys.test(keys);
        if (mounted.current) setCheck({ v, state: 'done', result });
      } catch (e) {
        if (!mounted.current) return;
        if (e instanceof ApiError && e.status === 429 && !isRetry) {
          // Throttled: wait what the server asked, then retry once if nothing changed.
          const ms = Math.min(Math.max(e.retryAfter ?? 2, 1), 30) * 1000;
          window.setTimeout(() => {
            if (mounted.current && latest.current.version === v) void run(true);
          }, ms);
        } else if (e instanceof ApiError && e.status === 429) {
          setCheck({ v, state: 'error', message: 'Plaid checks are busy right now. Wait a few seconds, then check again.' });
        } else if (!(e instanceof ApiError && e.status === 401)) {
          setCheck({ v, state: 'error', message: errorMessage(e) });
        }
      } finally {
        inFlight.current = false;
        if (mounted.current && queued.current) {
          queued.current = false;
          void run();
        }
      }
    },
    [schedule],
  );

  // Every edit bumps `version`; check the keys once typing/pasting pauses. Keys of Plaid's usual
  // lengths are checked quickly; other lengths wait longer, so a pause mid-typing isn't reported
  // as "didn't work".
  useEffect(() => {
    const keys = latest.current.input;
    if (!keys) return;
    const usual = keys.client_id.length === CLIENT_ID_LEN && keys.secret.length === SECRET_LEN;
    schedule(() => void run(), usual ? DEBOUNCE_MS : SLOW_DEBOUNCE_MS);
  }, [version, run, schedule]);

  function edited() {
    setVersion((n) => n + 1);
    setPwError(null);
  }

  function swap() {
    setClientId(secret);
    setSecret(clientId);
    edited();
  }

  // ------------------------------------------------------------ save

  const current = check && check.v === version ? check : null;
  const passed = current?.state === 'done' && current.result.ok ? current.result : null;
  const saveEnv: PlaidEnv = locked ? status.env! : (manualEnv ?? passed?.env ?? 'sandbox');

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!passed || !input || saving) return;
    if (!password) return setPwError('Enter your Iron Owl password.');
    setSaving(true);
    setPwError(null);
    try {
      const s = await api.plaidKeys.save({ client_id: input.client_id, secret: input.secret, env: saveEnv }, password);
      setSecret('');
      setPassword('');
      onSaved(s);
    } catch (err) {
      if (!mounted.current) return;
      setPassword('');
      const code = plaidKeysErrorCode(err);
      if (err instanceof ApiError && err.status === 401) setPwError('That’s not your Iron Owl password.');
      else if (err instanceof ApiError && err.status === 429) setPwError(`Too many attempts. Try again in ${err.retryAfter ?? 30} seconds.`);
      else if (code === 'managed_by_env') onManagedByEnv();
      else if (code && code !== 'not_set' && code !== 'items_changed') {
        // The password was right; the keys failed the server's re-check. Show it like a failed check.
        setCheck({
          v: version,
          state: 'done',
          result: { ok: false, code, message: errorMessage(err), plaid_code: plaidKeysErrorPlaidCode(err), at: new Date().toISOString(), env: saveEnv },
        });
      } else setPwError(errorMessage(err));
    } finally {
      if (mounted.current) setSaving(false);
    }
  }

  // ------------------------------------------------------------ field hints

  const onlyKeyChars = (s: string) => /^[A-Za-z0-9]*$/.test(s);
  const cidBadChars = !onlyKeyChars(clientId);
  const cidOff = clientId !== '' && (cidBadChars || clientId.length !== CLIENT_ID_LEN) && (touched.client || cidBadChars || clientId.length >= CLIENT_ID_LEN);
  const secBadChars = !onlyKeyChars(secret);
  const secOff = secret !== '' && (secBadChars || !KEY_RE.test(secret) || Math.abs(secret.length - SECRET_LEN) > 2) && (touched.secret || secBadChars || secret.length >= SECRET_LEN);
  const cidMsg = 'The Client ID is 24 letters and numbers — paste it from Developers → Keys.';
  const secMsg = 'The Secret is about 30 letters and numbers — paste it from Developers → Keys.';
  const showCidProblem = cidOff && !swapped && !same && !locked;
  const showSecProblem = secOff && !swapped && !same;
  const cidHintId = `${uid}-cid-hint`;

  return (
    <div className="bc-form">
      <div className="bc-fields">
        <div className="field">
          <label className="field-label" htmlFor={`${uid}-cid`}>
            Client ID
          </label>
          <input
            id={`${uid}-cid`}
            className="input input-lg bc-key-input"
            value={clientId}
            onChange={(e) => {
              setClientId(cleanKey(e.target.value));
              edited();
            }}
            onBlur={() => setTouched((t) => ({ ...t, client: true }))}
            disabled={locked || saving}
            readOnly={locked}
            autoComplete="one-time-code"
            data-1p-ignore
            data-lpignore="true"
            data-bwignore
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            inputMode="text"
            maxLength={200}
            aria-invalid={showCidProblem && (cidBadChars || !KEY_RE.test(clientId)) ? true : undefined}
            aria-describedby={cidHintId}
          />
          <span className={showCidProblem && (cidBadChars || !KEY_RE.test(clientId)) ? 'field-error' : 'field-hint'} id={cidHintId}>
            {locked ? 'Stays the same while you have linked banks.' : showCidProblem ? cidMsg : '24 letters and numbers.'}
          </span>
        </div>
        <PasswordField
          label="Secret"
          toggleNoun="secret"
          name="plaid-secret"
          // Keep password managers from saving or filling the Plaid secret (they ignore "off").
          autoComplete="one-time-code"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore
          value={secret}
          onChange={(e) => {
            setSecret(cleanKey(e.target.value));
            edited();
          }}
          onBlur={() => setTouched((t) => ({ ...t, secret: true }))}
          inputRef={secretRef}
          disabled={saving}
          maxLength={200}
          hint={showSecProblem && !(secBadChars || !KEY_RE.test(secret)) ? secMsg : showSecProblem ? undefined : 'About 30 letters and numbers. Sandbox and Production secrets are different.'}
          error={showSecProblem && (secBadChars || !KEY_RE.test(secret)) ? secMsg : null}
        />
      </div>

      {(swapped || same) && (
        <div className="bc-inline-note" role="status">
          <Icon name="alert" />
          <span>
            {swapped
              ? 'These look swapped: the Client ID is the shorter one.'
              : 'Both boxes have the same thing. Copy the Client ID and the Secret separately from Developers → Keys.'}
          </span>
          {swapped && (
            <button type="button" className="btn" onClick={swap}>
              <Icon name="arrows" />
              Swap them
            </button>
          )}
        </div>
      )}

      {locked ? (
        <p className="acct-note bc-note">
          <Icon name="info" />
          <span>
            Your {plural(status.linked_items, 'linked bank')} belong{status.linked_items === 1 ? 's' : ''} to this Plaid account. You can paste a new Secret; to switch
            Plaid accounts, first remove your banks under Linked institutions.
          </span>
        </p>
      ) : (
        <details className="bc-advanced">
          <summary>Advanced: choose manually</summary>
          <div className="bc-advanced-body">
            <span className="small muted" id={`${uid}-env-label`}>
              Which Plaid keys are these? Iron Owl normally works this out from the keys.
            </span>
            <div className="segmented bc-env-seg" role="group" aria-labelledby={`${uid}-env-label`}>
              {([null, 'sandbox', 'production'] as const).map((e) => (
                <button
                  key={e ?? 'auto'}
                  type="button"
                  aria-pressed={manualEnv === e}
                  disabled={saving}
                  onClick={() => {
                    if (manualEnv === e) return;
                    setManualEnv(e);
                    edited();
                  }}
                >
                  {e === null ? 'Detect automatically' : ENV_LONG[e]}
                </button>
              ))}
            </div>
          </div>
        </details>
      )}

      {intro}

      <CheckLine
        check={current}
        waiting={!!input && !current}
        onRetry={() => {
          if (!inFlight.current) void run();
        }}
      />

      {passed && (
        <form className="bc-save" onSubmit={save} noValidate aria-label="Save Plaid keys">
          <PasswordField
            label="Your Iron Owl password"
            hint="To keep your bank connection safe, confirm it’s you."
            autoComplete="current-password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              setPwError(null);
            }}
            inputRef={pwRef}
            disabled={saving}
            error={pwError}
          />
          <div className="bc-actions">
            <button type="submit" className="btn btn-primary btn-lg" disabled={saving}>
              {saving ? <Icon name="sync" className="spin" /> : <Icon name="link" />}
              {saving ? 'Saving…' : mode === 'setup' ? 'Save and connect' : 'Save new keys'}
            </button>
            {onCancel && (
              <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={saving}>
                Cancel
              </button>
            )}
          </div>
        </form>
      )}

      {!passed && onCancel && (
        <div className="bc-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

/** The live "Checking… / These keys work / didn't work" line under the fields. */
function CheckLine({ check, waiting, onRetry }: { check: Check | null; waiting: boolean; onRetry: () => void }) {
  let body: ReactNode = null;
  let tone = '';
  if (check?.state === 'checking' || (waiting && !check)) {
    tone = 'is-checking';
    body = (
      <>
        <Icon name="sync" className="spin" />
        <span>Checking your keys with Plaid…</span>
      </>
    );
  } else if (check?.state === 'done' && check.result.ok) {
    tone = 'is-ok';
    body = (
      <>
        <Icon name="check" />
        <span>
          <strong>{check.result.message}</strong>{' '}
          <button type="button" className="link-btn bc-again" onClick={onRetry}>
            Check again
          </button>
        </span>
      </>
    );
  } else if (check?.state === 'done' || check?.state === 'error') {
    tone = 'is-bad';
    const msg = check.state === 'done' ? check.result.message : check.message;
    const code = check.state === 'done' ? check.result.plaid_code : null;
    // Only say the keys are wrong when Plaid actually judged them.
    const judged = check.state === 'done' && keysRejected(check.result);
    body = (
      <>
        <Icon name="alert" />
        <span>
          <strong>{judged ? 'These keys didn’t work.' : 'Couldn’t check your keys.'}</strong> {msg}
          {code && (
            <details className="bc-details">
              <summary>Details</summary>
              <span className="small muted">
                Error code: <code>{code}</code>
              </span>
            </details>
          )}
        </span>
        <button type="button" className="btn bc-check-btn" onClick={onRetry}>
          <Icon name="sync" />
          Check again
        </button>
      </>
    );
  }
  return (
    <div className={`bc-check ${tone}`} aria-live="polite">
      {body}
    </div>
  );
}
