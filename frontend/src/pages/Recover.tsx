import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { activeSheet, api, ApiError, apiErrorCode, plainErrorMessage, rolledBackTo, triesLeft, typoGroups } from '../api';
import { useUpdates } from '../updates/UpdateProvider';
import { supportName, useApp } from '../state';
import { MIN_PASSWORD_LENGTH } from '../lib/strength';
import { GROUPS, badGroups, fullGroups, typoMessage } from '../lib/recoveryCode';
import { Icon } from '../components/Icon';
import { PasswordField } from '../components/ui';
import { RecoveryCodeInput } from '../components/recovery/RecoveryCodeInput';
import { formatWait, formatWaitShort, triesLine } from '../components/recovery/recoveryFormat';
import '../components/recovery/recovery.css';

type Step = 'code' | 'password' | 'done' | 'none';
interface Note {
  title: string;
  body: ReactNode;
}

const EMPTY: readonly string[] = ['', '', '', '', '', ''];

/**
 * "Forgot your password?" (design screens 4-5): type the 6 groups from the recovery sheet,
 * choose a new password, "You're back in". Shown inside the unlock screen, with no route or
 * URL of its own. The numbers live only in this component's state (and a ref between the two
 * steps) and are dropped on success, on Back, and when it unmounts.
 */
export function RecoverFlow({
  onBack,
  waitSec,
  startWait,
  waitIfLockedOut,
}: {
  onBack: () => void;
  waitSec: number;
  startWait: (seconds: number) => void;
  waitIfLockedOut: () => Promise<boolean>;
}) {
  const { enterUnlocked, authInfo, refreshAuthInfo, retryStatus } = useApp();
  const { enterRollingBack } = useUpdates();
  const support = supportName(authInfo);
  const [step, setStep] = useState<Step>(authInfo.recovery && !authInfo.recovery.available ? 'none' : 'code');
  const [boxes, setBoxes] = useState<string[]>([...EMPTY]);
  const codeRef = useRef('');
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);
  const [pw, setPw] = useState('');
  const [pw2, setPw2] = useState('');
  const headingRef = useRef<HTMLHeadingElement>(null);
  const progressId = useId();
  const sheet = authInfo.recovery?.sheet ?? null;

  // Fresh status: a sheet may have been made (or the vault restored) since this screen loaded.
  useEffect(() => {
    let alive = true;
    void refreshAuthInfo().then((info) => {
      if (alive && info.recovery && !info.recovery.available) setStep('none');
    });
    return () => {
      alive = false;
      codeRef.current = '';
    };
  }, [refreshAuthInfo]);

  // Headings take focus on the steps without a box to type in.
  useEffect(() => {
    if (step === 'done' || step === 'none') headingRef.current?.focus();
  }, [step]);

  function back() {
    codeRef.current = '';
    setBoxes([...EMPTY]);
    setPw('');
    setPw2('');
    setNote(null);
    onBack();
  }

  /** Error copy for the sheet, by the server's code. Returns null when handled another way. */
  async function sheetNote(e: unknown): Promise<Note | null> {
    if (e instanceof ApiError && e.status === 429) {
      startWait(e.retryAfter ?? 30);
      return null;
    }
    const code = apiErrorCode(e);
    // The server names the sheet that works now on no_match / outdated / unusable.
    const current = activeSheet(e) ?? sheet;
    const thisSheet = current ? ` This computer’s sheet is number ${current}.` : ' The computer’s name is written at the top of the sheet.';
    switch (code) {
      case 'typo': {
        const groups = typoGroups(e);
        return {
          title: groups.length ? typoMessage(groups) : 'One of the groups has a typo.',
          body: 'Check it against your sheet. One of its numbers is probably different.',
        };
      }
      case 'bad_format':
        return { title: 'Some numbers are missing.', body: 'Type all 6 groups of 6 numbers from your sheet.' };
      case 'no_match': {
        const n = triesLeft(e);
        if (n === 0 && (await waitIfLockedOut())) {
          return { title: 'Those numbers don’t match this Iron Owl.', body: `Make sure it’s the sheet for this computer.${thisSheet}` };
        }
        return {
          title: 'Those numbers don’t match this Iron Owl.',
          body: (
            <>
              <div className="rs-note-body">Make sure it’s the sheet for this computer.{thisSheet}</div>
              {triesLine(n) && <div className="rs-note-body">{triesLine(n)}</div>}
            </>
          ),
        };
      }
      case 'outdated': {
        return {
          title: 'This sheet is out of date.',
          body: `A newer sheet was made after this one. Try your newest sheet; check the “Made on” date at the top.${
            current ? ` This computer’s sheet is number ${current}.` : ''
          }`,
        };
      }
      case 'unusable':
        return {
          title: 'Iron Owl can’t use this sheet right now.',
          body: `The numbers are right, but Iron Owl couldn’t open your data with them. Call ${support} before trying anything else.`,
        };
      case 'no_recovery':
        setStep('none');
        return null;
      case 'not_initialized':
        retryStatus();
        return null;
      default:
        if (e instanceof ApiError && e.status === 0) return { title: 'Iron Owl isn’t responding.', body: 'Please wait a moment and try again.' };
        return { title: 'Something went wrong.', body: plainErrorMessage(e) };
    }
  }

  // ---------------------------------------------------------------- step 1: the numbers
  const typos = badGroups(boxes);
  const full = fullGroups(boxes);
  const ready = full === GROUPS && typos.length === 0;
  const waiting = waitSec > 0;
  const progress =
    typos.length > 0
      ? `Fix the ${typos.length === 1 ? 'group' : 'groups'} marked “Check this group” to continue.`
      : full === GROUPS
        ? 'All 6 groups entered.'
        : `${full} of 6 groups entered`;

  async function check(e: FormEvent) {
    e.preventDefault();
    if (!ready || busy || waiting) return;
    const code = boxes.join('');
    setBusy(true);
    setNote(null);
    try {
      await api.auth.recoverCheck(code);
      codeRef.current = code;
      setBoxes([...EMPTY]);
      setStep('password');
    } catch (err) {
      setNote(await sheetNote(err));
    } finally {
      setBusy(false);
    }
  }

  // ---------------------------------------------------------------- step 2: new password
  const longEnough = [...pw].length >= MIN_PASSWORD_LENGTH;
  const matches = pw.length > 0 && pw === pw2;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!longEnough || !matches || busy || waiting) return;
    if (!codeRef.current) {
      setStep('code');
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await api.auth.recover(codeRef.current, pw);
      codeRef.current = '';
      setPw('');
      setPw2('');
      setStep('done');
    } catch (err) {
      const code = apiErrorCode(err);
      if (code === 'weak_password') {
        setNote({ title: 'Please choose a longer password.', body: `Use at least ${MIN_PASSWORD_LENGTH} characters. A short sentence works well.` });
      } else if (err instanceof ApiError && err.status === 429) {
        startWait(err.retryAfter ?? 30);
      } else if (rolledBackTo(err) !== null) {
        // The update's data step failed: FinTrack goes back to the previous version by itself.
        codeRef.current = '';
        setPw('');
        setPw2('');
        enterRollingBack(rolledBackTo(err) ?? '');
      } else if (code && ['typo', 'bad_format', 'no_match', 'outdated', 'unusable', 'no_recovery', 'not_initialized'].includes(code)) {
        // The sheet stopped working in between (another sheet was made): back to the numbers.
        codeRef.current = '';
        setPw('');
        setPw2('');
        const n = await sheetNote(err);
        if (code !== 'no_recovery' && code !== 'not_initialized') setStep('code');
        setNote(n);
      } else {
        setNote({ title: 'Your new password wasn’t saved.', body: plainErrorMessage(err) });
      }
    } finally {
      setBusy(false);
    }
  }

  const noteBox = note && (
    <div className="rs-note rs-note-amber" role="alert">
      <span className="rs-note-ic" aria-hidden="true">
        <Icon name="alert" />
      </span>
      <div>
        <div className="rs-note-title">{note.title}</div>
        {typeof note.body === 'string' ? <div className="rs-note-body">{note.body}</div> : note.body}
      </div>
    </div>
  );
  const waitBox = waiting && (
    <div className="rs-note" aria-hidden="true">
      <div className="rs-note-body">Too many tries. Please wait {formatWait(waitSec)}, then try again.</div>
    </div>
  );

  if (step === 'none') {
    return (
      <div className="auth">
        <div className="auth-card auth-xl auth-480 rs-stack">
          <button type="button" className="back-btn" onClick={back}>
            <Icon name="chevronLeft" />
            Back to password
          </button>
          <h1 ref={headingRef} tabIndex={-1}>
            This Iron Owl doesn’t have a recovery sheet
          </h1>
          <p className="rs-lead">Without your password, Iron Owl can’t open your data.</p>
          <div className="rs-note rs-note-amber">
            <span className="rs-note-ic" aria-hidden="true">
              <Icon name="alert" />
            </span>
            <div>
              <div className="rs-note-title">Call {support} before trying anything else.</div>
              <div className="rs-note-body">Don’t reinstall Iron Owl or delete any files. That can’t bring your data back.</div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (step === 'done') {
    return (
      <div className="auth">
        <div className="auth-card auth-xl auth-480 rs-stack">
          <span className="rs-done-ic" aria-hidden="true">
            <Icon name="check" />
          </span>
          <div>
            <h1 ref={headingRef} tabIndex={-1}>
              You’re back in
            </h1>
            <p className="rs-lead">Your new password is saved. Your recovery sheet still works, so put it back with your important papers.</p>
          </div>
          <button type="button" className="btn btn-primary btn-xl btn-block" onClick={enterUnlocked}>
            Open Iron Owl
          </button>
        </div>
      </div>
    );
  }

  if (step === 'password') {
    return (
      <div className="auth">
        <div className="auth-card auth-xl auth-480">
          <div className="rs-pill">
            <Icon name="check" />
            Recovery sheet accepted
          </div>
          <h1 ref={headingRef} tabIndex={-1}>
            Choose a new password
          </h1>
          <p className="auth-sub">Pick something you’ll remember. A short sentence works well.</p>
          <form onSubmit={save} noValidate>
            <PasswordField size="xl" label="New password" autoComplete="new-password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus disabled={busy} />
            <PasswordField size="xl" label="Type it again" autoComplete="new-password" value={pw2} onChange={(e) => setPw2(e.target.value)} disabled={busy} />
            <ul className="pw-checks" aria-label="Your new password">
              <Check ok={longEnough}>At least {MIN_PASSWORD_LENGTH} characters</Check>
              <Check ok={matches}>Both boxes match</Check>
            </ul>
            {noteBox}
            {waitBox}
            <button type="submit" className="btn btn-primary btn-xl btn-block" disabled={!longEnough || !matches || busy || waiting}>
              {busy ? 'Saving your new password…' : waiting ? `Please wait ${formatWaitShort(waitSec)}` : 'Save new password'}
            </button>
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="auth">
      <section className="auth-page" aria-labelledby={`${progressId}-h`}>
        <button type="button" className="back-btn" onClick={back}>
          <Icon name="chevronLeft" />
          Back to password
        </button>
        <div>
          <h1 id={`${progressId}-h`} ref={headingRef} tabIndex={-1}>
            Use your recovery sheet
          </h1>
          <p className="auth-lead">Type the 6 groups of numbers from your sheet. You can also paste all of them into the first box.</p>
          {sheet && (
            <p className="auth-lead">
              Use the sheet numbered <strong>{sheet}</strong>. The number is at the top of the sheet.
            </p>
          )}
        </div>
        <form onSubmit={check} noValidate className="rs-stack">
          <RecoveryCodeInput
            value={boxes}
            onChange={(b) => {
              setBoxes(b);
              if (note) setNote(null);
            }}
            disabled={busy}
            describedBy={progressId}
            autoFocus
          />
          {noteBox}
          {waitBox}
          <div className="rc-foot">
            <span id={progressId} className="rc-progress" aria-live="polite">
              {progress}
            </span>
            <button type="submit" className="btn btn-primary btn-xl" aria-disabled={!ready || busy || waiting ? true : undefined}>
              {busy ? 'Checking…' : waiting ? `Please wait ${formatWaitShort(waitSec)}` : 'Continue'}
            </button>
          </div>
        </form>
        <details className="rs-details">
          <summary>I can’t find my sheet</summary>
          <p>
            Look with your important papers first. If you have more than one sheet, use the one with the newest date. Without your password or your
            sheet, Iron Owl can’t open your data, so call {support} before trying anything else.
          </p>
        </details>
      </section>
    </div>
  );
}

function Check({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <li className={ok ? 'is-ok' : undefined}>
      <span className="pw-check-ic" aria-hidden="true">
        {ok && <Icon name="check" />}
      </span>
      {children}
      <span className="sr-only">{ok ? ' (done)' : ' (not yet)'}</span>
    </li>
  );
}
