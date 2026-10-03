import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, ApiError, apiErrorCode, plainErrorMessage, triesLeft, type RecoveryPending, type RecoveryStatus } from '../../api';
import type { ApiState } from '../../lib/useApi';
import { useCountdown } from '../../lib/useCountdown';
import { Icon } from '../../components/Icon';
import { Modal } from '../../components/Modal';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { PasswordField } from '../../components/ui';
import { useToast } from '../../components/Toast';
import { RecoverySheetBody } from '../../components/recovery/RecoverySheetBody';
import { formatWait, sheetDate, triesLine } from '../../components/recovery/recoveryFormat';
import { StDialog } from './parts';

export interface RecoverySheetFlow {
  /** "Make a recovery sheet" / "Make a new sheet…" (design D3): password, then the new numbers. */
  make: () => void;
  /** "Finish the sheet…" (status `unconfirmed`): the user either has the sheet (confirm it) or makes a new one. */
  finish: () => void;
  /** The dialogs; render once. */
  dialogs: ReactNode;
}

/**
 * The recovery sheet dialogs (design D3), lifted out of the old Settings card so the Needs-you
 * card and Settings › Safety and backups share them. Making a sheet asks for the password,
 * shows the new numbers in a dialog that can't be closed by accident, and only replaces the
 * old sheet when Done is clicked (POST /api/recovery/activate). Cancel keeps the old sheet.
 */
export function useRecoverySheetFlow(recovery: ApiState<RecoveryStatus>): RecoverySheetFlow {
  const toast = useToast();
  const [step, setStep] = useState<'closed' | 'finish' | 'ask' | 'show'>('closed');
  const [password, setPassword] = useState('');
  const [pwError, setPwError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<RecoveryPending | null>(null);
  const [checked, setChecked] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [waitSec, startWait] = useCountdown(() => setPwError(null));
  const pwRef = useRef<HTMLInputElement>(null);
  const askFormId = useId();

  const st = recovery.data;
  const hasSheet = !!st && st.status !== 'none' && !!st.sheet;
  const oldSheet = hasSheet ? st!.sheet : null;

  function make() {
    setPassword('');
    setPwError(null);
    setStep('ask');
  }

  useEffect(() => {
    if (step !== 'ask') return;
    const h = requestAnimationFrame(() => pwRef.current?.focus());
    return () => cancelAnimationFrame(h);
  }, [step]);

  function closeAll() {
    setStep('closed');
    setPending(null);
    setChecked(false);
    setPassword('');
    setPwError(null);
    setConfirmStop(false);
  }

  async function confirmHave() {
    if (!st?.sheet || busy) return;
    setBusy(true);
    try {
      const s = await api.recovery.confirm(st.sheet);
      recovery.setData(s);
      closeAll();
      toast.push({ tone: 'success', title: `Recovery sheet ${s.sheet ?? st.sheet} is ready. Keep it with your important papers.` });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return;
      closeAll();
      recovery.reload();
      toast.push({
        tone: 'error',
        title: apiErrorCode(err) === 'sheet_changed' ? 'Your recovery sheet changed meanwhile' : 'Couldn’t finish the sheet',
        body: apiErrorCode(err) === 'sheet_changed' ? 'Settings now shows the sheet Iron Owl uses.' : plainErrorMessage(err),
      });
    } finally {
      setBusy(false);
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    if (busy || waitSec > 0) return;
    if (!password) {
      setPwError('Enter your Iron Owl password.');
      return;
    }
    setBusy(true);
    setPwError(null);
    try {
      const p = await api.recovery.create(password);
      setPassword('');
      setPending(p);
      setChecked(false);
      setStep('show');
    } catch (err) {
      setPassword('');
      const code = apiErrorCode(err);
      if (err instanceof ApiError && err.status === 429) {
        const s = err.retryAfter ?? 30;
        startWait(s);
        setPwError(`Too many tries. Please wait ${formatWait(s)}, then try again.`);
      } else if (err instanceof ApiError && err.status === 401 && err.detail !== 'locked') {
        setPwError(`That password isn’t right. ${triesLine(triesLeft(err)) ?? 'Try again.'}`);
      } else if (code === 'password_required') {
        setPwError('Enter your Iron Owl password.');
      } else if (!(err instanceof ApiError && err.status === 401)) {
        setPwError(plainErrorMessage(err));
      }
      requestAnimationFrame(() => pwRef.current?.focus());
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    if (!pending || !checked || busy) return;
    setBusy(true);
    try {
      const s = await api.recovery.activate(pending.pending_id);
      recovery.setData(s);
      const n = s.sheet ?? pending.sheet;
      toast.push({
        tone: 'success',
        title: oldSheet ? `New sheet ${n} is active. The old sheet no longer works.` : `Recovery sheet ${n} is active.`,
      });
      closeAll();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // FinTrack locked: the unlock screen takes over
      if (apiErrorCode(err) === 'pending_expired') {
        closeAll();
        toast.push({
          tone: 'warn',
          title: 'This new sheet wasn’t saved',
          body: oldSheet
            ? 'Iron Owl locked or waited too long before Done. Your old sheet still works. Please make it again.'
            : 'Iron Owl locked or waited too long before Done. Please make it again.',
          timeout: 0,
        });
        recovery.reload();
      } else if (apiErrorCode(err) === 'finish_on_unlock') {
        // The vault was re-encrypted but the last step failed: FinTrack is locked now, and the
        // next unlock finishes it. Whether the new sheet took shows in Settings after that.
        const n = pending.sheet;
        closeAll();
        toast.push({
          tone: 'warn',
          title: 'Iron Owl needs to restart this step',
          body: `Unlock Iron Owl, then check Settings: if it shows sheet ${n}, that’s your new sheet. Keep both sheets until then.`,
          timeout: 0,
        });
      } else {
        toast.push({ tone: 'error', title: 'The new sheet wasn’t saved', body: plainErrorMessage(err) });
      }
    } finally {
      setBusy(false);
    }
  }

  const made = st ? sheetDate(st.created_at) : null;

  const dialogs = (
    <>
      <StDialog open={step === 'finish'} title="Finish your recovery sheet" onClose={closeAll} busy={busy}>
        <p className="st-dialog-lead">
          Iron Owl made sheet {st?.sheet ?? ''}
          {made ? ` on ${made}` : ''}, but the last step wasn’t done. Did you print it, or write the numbers down?
        </p>
        <p className="st-help">If you have it, it works. If you’re not sure, make a new one. The old one then stops working.</p>
        <div className="st-dialog-actions">
          <button type="button" className="st-btn st-btn-plain st-btn-lg" onClick={closeAll} disabled={busy}>
            Cancel
          </button>
          <span className="st-spacer" />
          <button type="button" className="st-btn st-btn-outline st-btn-lg" onClick={make} disabled={busy}>
            Make a new sheet
          </button>
          <button type="button" className="st-btn st-btn-primary st-btn-lg" onClick={() => void confirmHave()} disabled={busy}>
            {busy ? 'Saving…' : 'Yes, I have it'}
          </button>
        </div>
      </StDialog>

      <Modal
        open={step === 'ask'}
        width={520}
        title={hasSheet ? 'Make a new recovery sheet?' : 'Make a recovery sheet'}
        onClose={closeAll}
        busy={busy}
        footer={
          <>
            <button type="button" className="btn btn-ghost rs-modal-btn" onClick={closeAll} disabled={busy}>
              Cancel
            </button>
            <button type="submit" form={askFormId} className="btn btn-primary rs-modal-btn" disabled={busy || waitSec > 0}>
              {busy ? 'Making your sheet…' : hasSheet ? 'Make new sheet' : 'Continue'}
            </button>
          </>
        }
      >
        <form id={askFormId} onSubmit={create} noValidate className="modal-form rs-stack rs-ask">
          {oldSheet && (
            <div className="rs-note rs-note-amber">
              <span className="rs-note-ic" aria-hidden="true">
                <Icon name="alert" />
              </span>
              <div className="rs-note-body">
                <strong>Your current sheet ({oldSheet}) will stop working once you click Done.</strong> Tear it up or write VOID on it once the new one is
                printed: it can still open backups made before today.
              </div>
            </div>
          )}
          <PasswordField
            label="To continue, enter your Iron Owl password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              if (pwError && waitSec === 0) setPwError(null);
            }}
            inputRef={pwRef}
            disabled={busy || waitSec > 0}
            error={waitSec > 0 ? `Too many tries. Please wait ${formatWait(waitSec)}, then try again.` : pwError}
            errorLive={waitSec === 0}
          />
        </form>
      </Modal>

      <Modal
        open={step === 'show' && !!pending}
        width={720}
        title="Your new recovery sheet"
        onClose={() => setConfirmStop(true)}
        dismissible={false}
        busy={busy}
        className="rs-show-modal"
        footer={
          <div className="rs-done-row">
            <button type="button" className="btn btn-ghost rs-modal-btn" onClick={() => setConfirmStop(true)} disabled={busy}>
              Cancel
            </button>
            {!checked && <span className="rs-continue-hint">Tick the box to finish.</span>}
            <button
              type="button"
              className="btn btn-primary rs-modal-btn"
              aria-disabled={!checked || busy ? true : undefined}
              onClick={() => void activate()}
            >
              {busy ? 'Saving…' : 'Done'}
            </button>
          </div>
        }
      >
        {pending && (
          <div className="rs-stack">
            <p className="rs-lead rs-lead-tight">Print this new sheet, or write the numbers down exactly as shown.</p>
            <RecoverySheetBody data={pending} checked={checked} onChecked={setChecked} compact />
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmStop}
        title="Stop making a new sheet?"
        confirmLabel="Stop"
        cancelLabel="Keep going"
        onCancel={() => setConfirmStop(false)}
        onConfirm={closeAll}
      >
        <p>
          {oldSheet
            ? `The numbers on this screen won’t work. Keep using sheet ${oldSheet}.`
            : 'The numbers on this screen won’t work, and you still won’t have a recovery sheet.'}
        </p>
      </ConfirmDialog>
    </>
  );

  return {
    make,
    finish: () => {
      if (st?.status === 'unconfirmed' && st.sheet) setStep('finish');
      else make();
    },
    dialogs,
  };
}
