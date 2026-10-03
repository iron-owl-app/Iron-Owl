import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, plainErrorMessage, type RecoverySheetData } from '../api';
import { useApp } from '../state';
import { Icon } from '../components/Icon';
import { PasswordField } from '../components/ui';
import { StrengthMeter } from '../components/StrengthMeter';
import { RestoreFields, useRestore } from '../components/BackupRestore';
import { RecoverySheetBody } from '../components/recovery/RecoverySheetBody';
import { MIN_PASSWORD_LENGTH } from '../lib/strength';
import { useCountdown } from '../lib/useCountdown';

export function SetupPage() {
  const [mode, setMode] = useState<'create' | 'restore'>('create');
  // The new vault's recovery sheet, shown once as step 2. Held only here, only until Continue.
  const [sheet, setSheet] = useState<RecoverySheetData | null>(null);
  if (sheet) return <RecoverySheetStep data={sheet} />;
  return mode === 'create' ? (
    <CreateVault onRestore={() => setMode('restore')} onSheet={setSheet} />
  ) : (
    <RestoreVault onBack={() => setMode('create')} />
  );
}

function StepProgress({ step }: { step: 1 | 2 }) {
  return (
    <div className="step-progress">
      <span className="bars" aria-hidden="true">
        <span className="on" />
        <span className={step === 2 ? 'on' : undefined} />
      </span>
      Step {step} of 2 · Setting up Iron Owl
    </div>
  );
}

/** Help (`#/help`) works before any vault exists; its Back button returns here. */
function SetupHelpLink() {
  return (
    <p className="auth-help">
      <Link className="link-btn" to="/help">
        <Icon name="help" />
        Help
      </Link>
    </p>
  );
}

function CreateVault({ onRestore, onSheet }: { onRestore: () => void; onSheet: (s: RecoverySheetData) => void }) {
  const { enterUnlocked, retryStatus } = useApp();
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touchedConfirm, setTouchedConfirm] = useState(false);
  const [waitSec, startWait] = useCountdown(() => setError(null));

  const tooShort = [...password].length < MIN_PASSWORD_LENGTH;
  const mismatch = confirm.length > 0 && confirm !== password;
  const canSubmit = !tooShort && confirm === password && understood && !busy && waitSec === 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.auth.setup(password);
      setPassword('');
      setConfirm('');
      // Step 2 prints the recovery sheet; an older server without one goes straight in.
      if (res.recovery && Array.isArray(res.recovery.groups) && res.recovery.groups.length === 6) onSheet(res.recovery);
      else enterUnlocked();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Already initialized (e.g. from another tab): go to the unlock screen.
        retryStatus();
        return;
      }
      if (err instanceof ApiError && err.status === 429) {
        startWait(err.retryAfter ?? 30);
        setError('Too many attempts. Please wait before trying again.');
      } else {
        setError(plainErrorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth-card wide">
        <div className="auth-lock" aria-hidden="true">
          <Icon name="shield" />
        </div>
        <h1>Set up Iron Owl</h1>
        <p className="auth-sub">Choose a master password. It encrypts everything Iron Owl stores on this computer.</p>

        <form onSubmit={submit} noValidate>
          <StepProgress step={1} />
          <div className="warning-box" role="note">
            <Icon name="note" />
            <div>
              <strong>Next, you’ll print a recovery sheet.</strong>
              <p>It’s the only way back in if you forget this password. Nobody can reset the password for you.</p>
            </div>
          </div>

          <PasswordField
            label="Master password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={MIN_PASSWORD_LENGTH}
            required
            autoFocus
            hint={<StrengthMeter password={password} />}
          />
          <PasswordField
            label="Confirm password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            onBlur={() => setTouchedConfirm(true)}
            required
            error={(touchedConfirm || confirm.length >= password.length) && mismatch ? 'Passwords don’t match.' : null}
          />

          <label className="check">
            <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            <span className="small">I understand that if I lose both this password and my recovery sheet, my Iron Owl data is gone for good.</span>
          </label>

          {error && (
            <div className="field-error" role="alert">
              {error}
            </div>
          )}

          <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={!canSubmit}>
            {busy ? 'Creating encrypted vault…' : waitSec > 0 ? `Try again in ${waitSec}s` : 'Create encrypted vault'}
          </button>
        </form>
        <div className="auth-alt">
          <span>Moving to a new computer?</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onRestore}>
            <Icon name="upload" />
            Restore from a backup
          </button>
        </div>
        <SetupHelpLink />
        <p className="auth-foot">
          <Icon name="lock" />
          Your data stays on this computer. Only Plaid is contacted, and only when you link or sync.
        </p>
      </div>
    </div>
  );
}

/**
 * Setup step 2 (design screen 1): the new vault's recovery sheet. The vault is already created
 * and unlocked; Continue (after the tick) marks the sheet confirmed and opens FinTrack. There's
 * no Back: the password is set. Closing the window here leaves the sheet "not finished", and
 * Home and Settings offer to make a new one.
 */
function RecoverySheetStep({ data }: { data: RecoverySheetData }) {
  const { enterUnlocked, authInfo } = useApp();
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);
  const contact = authInfo.supportContact;

  async function next() {
    if (!checked || busy) return;
    setBusy(true);
    try {
      await api.recovery.confirm(data.sheet);
    } catch {
      /* the sheet still works; its status stays "not finished" and Home offers a new one */
    }
    enterUnlocked();
  }

  return (
    <div className="auth">
      <section className="auth-page" aria-labelledby="rs-setup-h">
        <StepProgress step={2} />
        <div>
          <h1 id="rs-setup-h" ref={headingRef} tabIndex={-1}>
            Print your recovery sheet
          </h1>
          <p className="auth-lead">
            If you ever forget your password, this sheet is the only way back in.{' '}
            {contact ? `Nobody else can reset your password, not even ${contact}.` : 'Nobody can reset your password for you.'}
          </p>
        </div>
        <RecoverySheetBody data={data} checked={checked} onChecked={setChecked} />
        <div className="rs-continue">
          {!checked && (
            <p id="rs-setup-hint" className="rs-continue-hint">
              Tick the box above to continue.
            </p>
          )}
          <button
            type="button"
            className="btn btn-primary btn-xl"
            aria-disabled={!checked || busy ? true : undefined}
            aria-describedby={!checked ? 'rs-setup-hint' : undefined}
            onClick={() => void next()}
          >
            {busy ? 'Opening Iron Owl…' : 'Continue'}
          </button>
        </div>
      </section>
    </div>
  );
}

/** New computer: restore a .ftbackup before any vault exists (no session needed). */
function RestoreVault({ onBack }: { onBack: () => void }) {
  const { enterUnlocked, retryStatus } = useApp();
  const restore = useRestore({ requireConfirm: false });

  async function submit(e: FormEvent) {
    e.preventDefault();
    const res = await restore.submit();
    if (res === 'ok') enterUnlocked();
    // A vault exists after all (set up in another tab): go to the unlock screen.
    else if (res === 'initialized') window.setTimeout(retryStatus, 2500);
  }

  return (
    <div className="auth">
      <div className="auth-card wide">
        <div className="auth-lock" aria-hidden="true">
          <Icon name="upload" />
        </div>
        <h1>Restore from a backup</h1>
        <p className="auth-sub">Bring your Iron Owl data to this computer from a .ftbackup file.</p>
        <form onSubmit={submit} noValidate>
          <RestoreFields state={restore} requireConfirm={false} autoFocus />
          <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={!restore.canSubmit}>
            {restore.busy ? 'Restoring your vault…' : 'Restore and unlock'}
          </button>
        </form>
        <div className="auth-alt">
          <span>Starting fresh?</span>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onBack} disabled={restore.busy}>
            <Icon name="chevronLeft" />
            Create a new vault instead
          </button>
        </div>
        <SetupHelpLink />
        <p className="auth-foot">
          <Icon name="lock" />
          The backup stays encrypted until it’s unpacked on this computer with your password.
        </p>
      </div>
    </div>
  );
}
