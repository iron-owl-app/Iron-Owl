import { useId, useState, type ChangeEvent } from 'react';
import { api, ApiError, errorMessage } from '../api';
import { Icon } from './Icon';
import { PasswordField } from './ui';

/** The typed confirmation for replacing data from Settings. */
export const RESTORE_WORD = 'REPLACE';

export interface RestoreState {
  file: File | null;
  password: string;
  /** Only when replacing an existing vault (Settings). */
  currentPassword: string;
  confirmText: string;
  busy: boolean;
  error: { field: 'file' | 'password' | 'current' | 'form'; msg: string } | null;
  setFile: (f: File | null) => void;
  setPassword: (p: string) => void;
  setCurrentPassword: (p: string) => void;
  setConfirmText: (t: string) => void;
  canSubmit: boolean;
  /**
   * Uploads the backup. 'ok': the server is unlocked with the restored vault.
   * 'initialized': a vault already exists and there's no session (restore from Settings instead).
   */
  submit: () => Promise<'ok' | 'failed' | 'initialized'>;
  reset: () => void;
}

/** Shared by Settings (current password; `requireConfirm` adds the typed word) and the Setup screen (new computer). */
export function useRestore({ requireConfirm, needsCurrent = requireConfirm }: { requireConfirm: boolean; needsCurrent?: boolean }): RestoreState {
  const [file, setFileState] = useState<File | null>(null);
  const [password, setPasswordState] = useState('');
  const [currentPassword, setCurrentPasswordState] = useState('');
  const [confirmText, setConfirmText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<RestoreState['error']>(null);

  const confirmed = !requireConfirm || confirmText.trim().toUpperCase() === RESTORE_WORD;
  // Settings replaces an existing vault, which also needs that vault's current password
  // (Settings › Safety and backups asks in its own window, without the typed word).
  const canSubmit = !!file && password.length > 0 && (!needsCurrent || currentPassword.length > 0) && confirmed && !busy;

  async function submit(): Promise<'ok' | 'failed' | 'initialized'> {
    if (!file) {
      setError({ field: 'file', msg: 'Choose a .ftbackup file.' });
      return 'failed';
    }
    if (!password) {
      setError({ field: 'password', msg: 'Enter the master password this backup was made with.' });
      return 'failed';
    }
    if (needsCurrent && !currentPassword) {
      setError({ field: 'current', msg: 'Enter your current Iron Owl password.' });
      return 'failed';
    }
    if (!confirmed) return 'failed';
    setBusy(true);
    setError(null);
    try {
      await api.backup.restore(file, password, needsCurrent ? currentPassword : undefined);
      setPasswordState('');
      setCurrentPasswordState('');
      setConfirmText('');
      return 'ok';
    } catch (e) {
      setPasswordState('');
      setCurrentPasswordState('');
      if (e instanceof ApiError && (e.detail.includes('current Iron Owl password') || (e.status === 422 && needsCurrent))) {
        setError({ field: 'current', msg: e.detail });
        return 'failed';
      }
      if (e instanceof ApiError && (e.status === 409 || (e.status === 401 && e.detail === 'locked'))) {
        setError({ field: 'form', msg: 'Iron Owl already has a vault on this computer. Unlock it, then restore from Settings.' });
        return 'initialized';
      }
      if (e instanceof ApiError && e.status === 401) setError({ field: 'password', msg: 'That password doesn’t open this backup.' });
      else if (e instanceof ApiError && e.status === 429) setError({ field: 'form', msg: `Too many attempts. Try again in ${e.retryAfter ?? 30} seconds.` });
      else if (e instanceof ApiError && e.status === 400) setError({ field: 'file', msg: `This file can’t be restored: ${errorMessage(e)}` });
      else if (e instanceof ApiError && e.status === 413) setError({ field: 'file', msg: 'This file is too large to be an Iron Owl backup (200 MB max).' });
      else setError({ field: 'form', msg: errorMessage(e) });
      return 'failed';
    } finally {
      setBusy(false);
    }
  }

  return {
    file,
    password,
    confirmText,
    busy,
    error,
    setFile: (f) => {
      setFileState(f);
      setError((e) => (e?.field === 'file' ? null : e));
    },
    setPassword: (p) => {
      setPasswordState(p);
      setError((e) => (e?.field === 'password' ? null : e));
    },
    currentPassword,
    setCurrentPassword: (p) => {
      setCurrentPasswordState(p);
      setError((e) => (e?.field === 'current' ? null : e));
    },
    setConfirmText,
    canSubmit,
    submit,
    reset: () => {
      setFileState(null);
      setPasswordState('');
      setCurrentPasswordState('');
      setConfirmText('');
      setError(null);
      setBusy(false);
    },
  };
}

export function RestoreFields({ state, requireConfirm, autoFocus }: { state: RestoreState; requireConfirm: boolean; autoFocus?: boolean }) {
  const uid = useId();
  const onFile = (e: ChangeEvent<HTMLInputElement>) => state.setFile(e.target.files?.[0] ?? null);
  const fileErr = state.error?.field === 'file' ? state.error.msg : null;

  return (
    <div className="restore-fields">
      <div className="field">
        <label className="field-label" htmlFor={`${uid}-file`}>
          Backup file
        </label>
        <input
          id={`${uid}-file`}
          type="file"
          accept=".ftbackup,application/octet-stream"
          className="file-input"
          onChange={onFile}
          disabled={state.busy}
          autoFocus={autoFocus}
          aria-invalid={fileErr ? true : undefined}
          aria-describedby={fileErr ? `${uid}-file-err` : `${uid}-file-hint`}
        />
        {fileErr ? (
          <span className="field-error" id={`${uid}-file-err`} role="alert">
            {fileErr}
          </span>
        ) : (
          <span className="field-hint" id={`${uid}-file-hint`}>
            A <code>.ftbackup</code> file downloaded from Iron Owl’s Settings.
          </span>
        )}
      </div>
      <PasswordField
        label="Master password for this backup"
        autoComplete="current-password"
        value={state.password}
        onChange={(e) => state.setPassword(e.target.value)}
        disabled={state.busy}
        hint="The password you used when the backup was made. It becomes your password from now on."
        error={state.error?.field === 'password' ? state.error.msg : null}
      />
      {requireConfirm && (
        <>
          <PasswordField
            label="Your current Iron Owl password"
            autoComplete="current-password"
            value={state.currentPassword}
            onChange={(e) => state.setCurrentPassword(e.target.value)}
            disabled={state.busy}
            hint="Required to replace the vault you’re using now."
            error={state.error?.field === 'current' ? state.error.msg : null}
          />
          <div className="warning-box restore-warning" role="note">
            <Icon name="alert" />
            <div>
              <strong>This replaces everything in Iron Owl</strong>
              <p>
                Accounts, transactions, budgets, goals, rules and settings on this computer are swapped for the backup’s. Your current data is moved to a
                <code> pre-restore</code> folder next to the vault, not deleted.
              </p>
            </div>
          </div>
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-confirm`}>
              Type <strong>{RESTORE_WORD}</strong> to confirm
            </label>
            <input
              id={`${uid}-confirm`}
              className="input"
              value={state.confirmText}
              onChange={(e) => state.setConfirmText(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              autoCapitalize="characters"
              disabled={state.busy}
            />
          </div>
        </>
      )}
      {state.error?.field === 'form' && (
        <p className="field-error" role="alert">
          {state.error.msg}
        </p>
      )}
    </div>
  );
}
