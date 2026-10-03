import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, apiErrorCode, plainErrorMessage, rolledBackTo, triesLeft } from '../../api';
import { useUpdates } from '../../updates/UpdateProvider';
import { MESSAGES } from '../../updates/copy';
import { useApp } from '../../state';
import { useCountdown } from '../../lib/useCountdown';
import { PasswordField } from '../ui';
import { RecoverFlow } from '../../pages/Recover';
import { clearRecoverRequest, hasRecoverRequest } from '../../lib/recoverRequest';
import { formatWait, triesLine } from '../recovery/recoveryFormat';
import { GateCard } from './GateCard';
import { AFTER_WAIT, lockoutLine, passwordCopy, waitButton, wrongPasswordLine } from './gateCopy';
import '../recovery/recovery.css';

/**
 * Sign in and Locked (design D4 states 2 and 6) as one screen; `lockReason` from the app state
 * picks the heading and message (start | idle | closed | manual | ended | here).
 *
 * Behind "Forgot your password?" is the recovery sheet flow (pages/Recover.tsx). Both share one
 * "too many tries" wait: the server counts wrong passwords and wrong sheets together, and a
 * reload picks the wait up again from /api/auth/status.
 */
export function PasswordScreen() {
  const { enterUnlocked, lockReason, autoLockMinutes, refreshAuthInfo, retryStatus, authInfo } = useApp();
  const { enterRollingBack } = useUpdates();
  const copy = passwordCopy(lockReason, autoLockMinutes);
  const titleId = useId();
  // Settings › Change password › "use your recovery sheet" locks and lands here on the sheet steps.
  const [mode, setMode] = useState<'password' | 'recover'>(() => (hasRecoverRequest() ? 'recover' : 'password'));
  useEffect(() => clearRecoverRequest(), []);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Wrong tries already made (e.g. before a reload): "You have 3 tries left…" under the box.
  const [triesHint, setTriesHint] = useState<number | null>(null);
  // How long the current wait is (the note says it once, in words); the button counts down.
  const [waitLen, setWaitLen] = useState(0);
  // After a wait: "You can try again now. Another wrong try means a longer wait."
  const [afterWait, setAfterWait] = useState(false);
  // Screen-reader news: the wait starting (recovery steps only; here the note takes focus) and ending.
  const [waitNews, setWaitNews] = useState<{ text: string; kind: 'start' | 'end' } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const waitNoteRef = useRef<HTMLDivElement>(null);
  // Set when a wait ends: the box is focused again once it is enabled (after that render).
  const [refocus, setRefocus] = useState(false);
  const [waitSec, startCountdown] = useCountdown(() => {
    setWaitNews({ text: AFTER_WAIT, kind: 'end' });
    setAfterWait(true);
    setError(null);
    setRefocus(true);
  });

  useEffect(() => {
    if (!refocus || waitSec > 0 || mode !== 'password') return;
    setRefocus(false);
    inputRef.current?.focus();
  }, [refocus, waitSec, mode]);

  const startWait = useCallback(
    (seconds: number) => {
      startCountdown(seconds);
      setWaitLen(seconds);
      setAfterWait(false);
      setPassword('');
      setWaitNews({ text: `Too many tries. Please wait ${formatWait(seconds)}, then try again.`, kind: 'start' });
      // The box is disabled while waiting, so focus goes to the note (it's read out once).
      window.requestAnimationFrame(() => waitNoteRef.current?.focus());
    },
    [startCountdown],
  );

  /** After the last try before a wait: ask the server how long the wait is. */
  const waitIfLockedOut = useCallback(async () => {
    const info = await refreshAuthInfo();
    if (info.retryAfter) startWait(info.retryAfter);
    return info.retryAfter !== null;
  }, [refreshAuthInfo, startWait]);

  // A reload during a wait keeps waiting (the server still refuses until it's over).
  useEffect(() => {
    let alive = true;
    void refreshAuthInfo().then((info) => {
      if (!alive) return;
      if (info.retryAfter) startWait(info.retryAfter);
      else if (info.triesLeft === 0) setAfterWait(true);
      else if (info.triesLeft !== null && info.triesLeft > 0 && info.triesLeft < 5) setTriesHint(info.triesLeft);
    });
    return () => {
      alive = false;
    };
  }, [refreshAuthInfo, startWait]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || waitSec > 0) return;
    if (!password) {
      inputRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.auth.unlock(password);
      setPassword('');
      enterUnlocked();
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        setError(null);
        startWait(err.retryAfter ?? 30);
      } else if (err instanceof ApiError && err.status === 401) {
        const n = triesLeft(err);
        setTriesHint(null);
        setAfterWait(false);
        if (n === 0 && (await waitIfLockedOut())) {
          setError(null);
        } else {
          setError(wrongPasswordLine(n));
          window.requestAnimationFrame(() => inputRef.current?.select());
        }
      } else if (err instanceof ApiError && apiErrorCode(err) === 'not_initialized') {
        retryStatus();
      } else if (rolledBackTo(err) !== null) {
        // The update's data step failed: FinTrack goes back to the previous version by itself.
        setPassword('');
        enterRollingBack(rolledBackTo(err) ?? '');
      } else if (err instanceof ApiError && err.status === 409 && (err.detail === 'schema_too_new' || apiErrorCode(err) === 'schema_too_new')) {
        setError(MESSAGES.tooNew(authInfo.supportContact));
      } else {
        setError(plainErrorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }

  if (mode === 'recover') {
    return (
      <>
        <RecoverFlow
          onBack={() => {
            setWaitNews((n) => (n?.kind === 'end' ? null : n));
            setMode('password');
            window.requestAnimationFrame(() => inputRef.current?.focus());
          }}
          waitSec={waitSec}
          startWait={startWait}
          waitIfLockedOut={waitIfLockedOut}
        />
        <p className="sr-only" role="status">
          {waitNews?.text ?? ''}
        </p>
      </>
    );
  }

  const waiting = waitSec > 0;
  return (
    <GateCard width={440} icon={copy.icon} title={copy.title} titleId={titleId}>
      <p className="gate-body">{copy.body}</p>
      <form className="gate-form" onSubmit={submit} noValidate aria-labelledby={titleId}>
        <PasswordField
          size="xl"
          label="Password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            if (error) setError(null);
          }}
          autoFocus
          required
          inputRef={inputRef}
          disabled={waiting}
          error={waiting ? null : error}
          hint={!waiting && !error && !afterWait && triesHint !== null ? triesLine(triesHint) : undefined}
        />
        {waiting && (
          <div className="gate-note is-neutral" ref={waitNoteRef} tabIndex={-1}>
            {lockoutLine(waitLen)}
          </div>
        )}
        {!waiting && afterWait && !error && <div className="gate-note is-neutral">{AFTER_WAIT}</div>}
        <button type="submit" className="btn btn-primary btn-xl btn-block gate-main-btn" disabled={busy || waiting}>
          {busy ? copy.busy : waiting ? waitButton(waitSec) : copy.button}
        </button>
        <button
          type="button"
          className="link-btn gate-link"
          onClick={() => {
            setPassword('');
            setError(null);
            // The hidden "You can try again now…" belongs to this screen: don't carry it over.
            setWaitNews((n) => (n?.kind === 'end' ? null : n));
            setMode('recover');
          }}
        >
          Forgot your password?
        </button>
        <Link className="link-btn gate-link gate-help-link" to="/help">
          Help
        </Link>
      </form>
      <p className="sr-only" role="status">
        {waitNews?.kind === 'end' ? waitNews.text : ''}
      </p>
    </GateCard>
  );
}
