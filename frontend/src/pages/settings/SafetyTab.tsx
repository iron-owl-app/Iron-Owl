import { useEffect, useId, useRef, useState, type ChangeEvent } from 'react';
import {
  api,
  ApiError,
  AUTO_LOCK_CHOICES,
  plainErrorMessage,
  saveBlob,
  triesLeft,
  type AutoBackup,
  type AutoLockMinutes,
  type PreviousVault,
  type RecoveryStatus,
} from '../../api';
import { useApp } from '../../state';
import { useApi } from '../../lib/useApi';
import { formatDateTime, toISODate } from '../../lib/format';
import { MIN_PASSWORD_LENGTH } from '../../lib/strength';
import { requestRecoverOnLock } from '../../lib/recoverRequest';
import { Icon } from '../../components/Icon';
import { PasswordField, Skeleton } from '../../components/ui';
import { StrengthMeter } from '../../components/StrengthMeter';
import { useRestore } from '../../components/BackupRestore';
import { useToast } from '../../components/Toast';
import { sheetDate, triesLine } from '../../components/recovery/recoveryFormat';
import { announceSettingsChanged, useSettings } from './context';
import { Chip, DialogActions, StDialog, StOptions, StSwitch, useFail, useSay, type ChipTone } from './parts';
import { SupportContactCard } from './SupportContactCard';

/**
 * Settings › Safety and backups (design D7 tab 6): Backups (save one now, restore, automatic
 * backups with folder and how many to keep, and "More backup options" for the backup files
 * and vaults set aside by earlier restores), then Password and safety (lock now, auto-lock,
 * change password, recovery sheet), then Who to call for help (Iron Owl 2.0.0).
 */
export function SafetyTab() {
  return (
    <>
      <BackupsCard />
      <SafetyCard />
      <SupportContactCard />
    </>
  );
}

// ---------------------------------------------------------------- Backups

const KEEP_CHOICES = [5, 10, 20] as const;

/** "C:\Users\you\OneDrive\Iron Owl backups" → "OneDrive › Iron Owl backups". */
export function prettyFolder(dir: string): string {
  const parts = dir.split(/[\\/]+/).filter(Boolean);
  // In the user's own folder (C:\Users\name\...): the part after it. Anywhere else: the full path.
  if (parts.length > 3 && /^users$/i.test(parts[1] ?? '')) return parts.slice(3).join(' › ');
  return dir;
}

function lastBackupText(a: AutoBackup): string {
  if (!a.last_at) return 'No backup yet. The first one is saved the next time Iron Owl locks.';
  const d = new Date(a.last_at);
  if (Number.isNaN(d.getTime())) return 'Last backup: not known';
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  const today = toISODate(new Date());
  const yest = toISODate(new Date(Date.now() - 86_400_000));
  const day = toISODate(d) === today ? 'today' : toISODate(d) === yest ? 'yesterday' : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return `Last backup: ${day} at ${time}`;
}

export function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function BackupsCard() {
  const { backup, turnOnBackups, turningOn, typeFolderAsk, clearTypeFolderAsk, recovery } = useSettings();
  const { reloadUnlocked } = useApp();
  const toast = useToast();
  const say = useSay();
  const fail = useFail();
  const uid = useId();
  const a = backup.data;
  const on = !!a?.dir;
  const [saving, setSaving] = useState(false);
  const [picking, setPicking] = useState(false);

  // Save a backup now (keeps its password step).
  const [dlOpen, setDlOpen] = useState(false);
  const [dlPw, setDlPw] = useState('');
  const [dlErr, setDlErr] = useState<string | null>(null);
  const [dlBusy, setDlBusy] = useState(false);

  // Restore: confirm, pick the file, then the two passwords.
  const [restoreAsk, setRestoreAsk] = useState(false);
  const restore = useRestore({ requireConfirm: false, needsCurrent: true });
  const [restoreOpen, setRestoreOpen] = useState(false);

  // Change folder when the Windows picker isn't available (not on Windows).
  const [typeDir, setTypeDir] = useState<string | null>(null);
  const [typeErr, setTypeErr] = useState<string | null>(null);

  // "Turn on" found no OneDrive or Documents folder and there's no folder window here:
  // ask for the path instead.
  useEffect(() => {
    if (!typeFolderAsk) return;
    clearTypeFolderAsk();
    setTypeErr(null);
    setTypeDir('');
  }, [typeFolderAsk, clearTypeFolderAsk]);

  async function download() {
    if (!dlPw) return setDlErr('Type your Iron Owl password.');
    setDlBusy(true);
    setDlErr(null);
    try {
      const blob = await api.backup.download(dlPw);
      saveBlob(blob, `iron-owl-backup-${toISODate(new Date())}.ftbackup`);
      setDlOpen(false);
      setDlPw('');
      say('Backup saved to your Downloads folder.');
    } catch (e) {
      setDlPw('');
      if (e instanceof ApiError && e.status === 401) setDlErr(`That isn’t your password now. ${triesLine(triesLeft(e)) ?? 'Try again.'}`);
      else if (e instanceof ApiError && e.status === 429) setDlErr(`Too many tries. Please wait ${e.retryAfter ?? 30} seconds, then try again.`);
      else setDlErr(plainErrorMessage(e));
    } finally {
      setDlBusy(false);
    }
  }

  function onPickBackup(e: ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    e.target.value = '';
    if (!f) return;
    restore.reset();
    restore.setFile(f);
    setRestoreAsk(false);
    setRestoreOpen(true);
  }

  async function doRestore() {
    if ((await restore.submit()) !== 'ok') return;
    setRestoreOpen(false);
    restore.reset();
    const sheetOk = recovery.data?.status === 'active';
    toast.push({
      tone: 'success',
      title: 'Backup restored',
      body: sheetOk
        ? 'Iron Owl now shows the backup’s data. From now on, open it with the password that backup was made with. Your recovery sheet still works.'
        : 'Iron Owl now shows the backup’s data. From now on, open it with the password that backup was made with. Please make a new recovery sheet.',
      timeout: sheetOk ? undefined : 0,
    });
    reloadUnlocked();
  }

  async function turnOff() {
    if (!a) return;
    setSaving(true);
    try {
      backup.setData(await api.autoBackup.set(null, a.keep));
      say('Automatic backups are off. Backups already in the folder stay there.');
      announceSettingsChanged();
    } catch (e) {
      fail('Couldn’t turn off automatic backups', e);
    } finally {
      setSaving(false);
    }
  }

  async function setDir(dir: string): Promise<string | null> {
    const wasOn = on;
    try {
      const saved = await api.autoBackup.set(dir, a?.keep ?? 10, true);
      backup.setData(saved);
      // Only what the server confirms: never "on" without a folder.
      if (!saved.dir) return 'Iron Owl couldn’t save the backup folder. Please try again.';
      say(wasOn ? `Backups will be saved to ${prettyFolder(saved.dir)}.` : `Automatic backups are on. They’ll be saved to ${prettyFolder(saved.dir)}.`);
      if (!wasOn) announceSettingsChanged();
      return null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null;
      return plainErrorMessage(e);
    }
  }

  async function changeFolder() {
    setPicking(true);
    try {
      const { dir } = await api.autoBackup.pickFolder();
      if (dir) {
        const err = await setDir(dir);
        if (err) toast.push({ tone: 'error', title: 'Iron Owl can’t save backups there', body: err });
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) {
        setTypeErr(null);
        setTypeDir(a?.dir ?? '');
      } else if (e instanceof ApiError && e.status === 409) {
        toast.push({ tone: 'info', title: 'A folder window is already open', body: 'Look for it behind Iron Owl, pick a folder there, then click Select Folder.' });
      } else fail('Couldn’t open the folder window', e);
    } finally {
      setPicking(false);
    }
  }

  async function setKeep(n: number) {
    if (!a?.dir || n === a.keep) return;
    const prev = a;
    backup.setData({ ...a, keep: n });
    try {
      backup.setData(await api.autoBackup.set(a.dir, n));
    } catch (e) {
      backup.setData(prev);
      fail('Couldn’t change how many to keep', e);
    }
  }

  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Backups</h2>
          <p className="st-desc">A backup is a copy of all your Iron Owl data in one file. It only opens with your Iron Owl password.</p>
        </div>
      </div>
      <div className="st-tiles">
        <div className="st-tile">
          <span>
            <span className="st-tile-title">Save a backup now</span>
            <span className="st-help">Saves one file to your Downloads folder.</span>
          </span>
          <button type="button" className="st-btn st-btn-outline" onClick={() => setDlOpen(true)}>
            Save a backup
          </button>
        </div>
        <div className="st-tile">
          <span>
            <span className="st-tile-title">Restore from a backup</span>
            <span className="st-help">Puts a backup’s data back on this computer. What’s here now is set aside, not deleted.</span>
          </span>
          <button type="button" className="st-btn st-btn-plain" onClick={() => setRestoreAsk(true)}>
            Restore…
          </button>
        </div>
      </div>

      {backup.loading && !a ? (
        <div className="st-loading" aria-busy="true" aria-label="Loading automatic backups">
          <Skeleton height={48} style={{ borderRadius: 10 }} />
        </div>
      ) : backup.error && !a ? (
        <p className="st-error">
          Iron Owl couldn’t check automatic backups.{' '}
          <button type="button" className="st-link st-link-inline" onClick={backup.reload}>
            Try again
          </button>
        </p>
      ) : (
        a && (
          <>
            <div className="st-row">
              <span className="st-row-text">
                <span className="st-label">Automatic backups</span>
                <span className="st-help" id={`${uid}-auto`}>
                  Saves a copy each time Iron Owl locks, and once a day.
                </span>
              </span>
              <StSwitch
                checked={on}
                onChange={(next) => void (next ? turnOnBackups() : turnOff())}
                label="Automatic backups"
                describedBy={`${uid}-auto`}
                disabled={saving || turningOn}
              />
            </div>
            {on && (
              <>
                <div className="st-row">
                  <span className="st-row-text">
                    <span className="st-label">Saved to</span>
                    <span className="st-value" title={a.dir ?? undefined}>
                      {prettyFolder(a.dir!)}
                    </span>
                    <span className="st-help-faint">{lastBackupText(a)}</span>
                    {a.last_error && <span className="st-warn-line">The last automatic backup didn’t work. {a.last_error} Iron Owl tries again next time it locks.</span>}
                  </span>
                  <button type="button" className="st-btn st-btn-plain" onClick={() => void changeFolder()} disabled={picking}>
                    {picking ? 'Choosing…' : 'Change folder…'}
                  </button>
                </div>
                <div className="st-row">
                  <span className="st-row-text">
                    <span className="st-label">How many to keep</span>
                    <span className="st-help">Older automatic backups are removed.</span>
                  </span>
                  <StOptions
                    label="Backups to keep"
                    value={KEEP_CHOICES.includes(a.keep as 5) ? a.keep : null}
                    options={KEEP_CHOICES.map((n) => ({ value: n, label: `Last ${n}` }))}
                    onChange={(n) => void setKeep(n)}
                  />
                </div>
              </>
            )}
          </>
        )
      )}
      <p className="st-note st-note-top">
        <Icon name="lock" />
        <span>Each backup opens with the password you had when it was made. If you change your password, older backups still need the old one.</span>
      </p>
      <MoreBackupOptions backup={a} />

      <StDialog
        open={dlOpen}
        size={480}
        title="Save a backup"
        onClose={() => {
          setDlOpen(false);
          setDlPw('');
          setDlErr(null);
        }}
        onSubmit={() => void download()}
        busy={dlBusy}
      >
        <p className="st-dialog-lead">Iron Owl saves one file to your Downloads folder. To make it, type your Iron Owl password.</p>
        <PasswordField
          label="Your Iron Owl password"
          autoComplete="current-password"
          value={dlPw}
          onChange={(e) => {
            setDlPw(e.target.value);
            setDlErr(null);
          }}
          error={dlErr}
          disabled={dlBusy}
          autoFocus
        />
        <DialogActions
          onCancel={() => {
            setDlOpen(false);
            setDlPw('');
            setDlErr(null);
          }}
          submitLabel={dlBusy ? 'Saving…' : 'Save a backup'}
          busy={dlBusy}
        />
      </StDialog>

      <StDialog open={restoreAsk} size={540} alert title="Restore from a backup?" onClose={() => setRestoreAsk(false)}>
        <p className="st-dialog-lead">
          Iron Owl will show the data from the backup instead of what’s here now. What’s here now is set aside, not deleted, so you can switch back.
        </p>
        <p className="st-help">You’ll need the password you had when the backup was made.</p>
        <div className="st-dialog-actions">
          <button type="button" className="st-btn st-btn-plain st-btn-lg" onClick={() => setRestoreAsk(false)}>
            Cancel
          </button>
          <label className="st-btn st-btn-primary st-btn-lg">
            Choose the backup file…
            <input type="file" accept=".ftbackup" className="st-file-input" onChange={onPickBackup} />
          </label>
        </div>
      </StDialog>

      <StDialog
        open={restoreOpen}
        title="Restore this backup"
        onClose={() => {
          setRestoreOpen(false);
          restore.reset();
        }}
        onSubmit={() => void doRestore()}
        busy={restore.busy}
      >
        <p className="st-dialog-lead">
          File: <strong className="st-strong">{restore.file?.name}</strong>
        </p>
        {restore.error?.field === 'file' && (
          <p className="st-alert is-plain" role="alert">
            {restore.error.msg}
          </p>
        )}
        <PasswordField
          label="The password this backup was made with"
          autoComplete="off"
          value={restore.password}
          onChange={(e) => restore.setPassword(e.target.value)}
          hint="From now on, you’ll open Iron Owl with this password."
          error={restore.error?.field === 'password' ? restore.error.msg : null}
          disabled={restore.busy}
          autoFocus
        />
        <PasswordField
          label="Your password now"
          autoComplete="current-password"
          value={restore.currentPassword}
          onChange={(e) => restore.setCurrentPassword(e.target.value)}
          error={restore.error?.field === 'current' ? restore.error.msg : null}
          disabled={restore.busy}
        />
        {restore.error?.field === 'form' && (
          <p className="st-alert is-plain" role="alert">
            {restore.error.msg}
          </p>
        )}
        <DialogActions
          onCancel={() => {
            setRestoreOpen(false);
            restore.reset();
          }}
          submitLabel={restore.busy ? 'Restoring…' : 'Restore'}
          submitDisabled={!restore.canSubmit}
          busy={restore.busy}
        />
      </StDialog>

      <StDialog open={typeDir !== null} title="Choose a backup folder" onClose={() => setTypeDir(null)} onSubmit={() => void saveTyped()} busy={saving}>
        <p className="st-dialog-lead">Type the full path of a folder on this computer.</p>
        <div className="st-field">
          <label className="st-field-label" htmlFor={`${uid}-dir`}>
            Folder
          </label>
          <input
            id={`${uid}-dir`}
            className="st-input"
            value={typeDir ?? ''}
            spellCheck={false}
            autoComplete="off"
            placeholder="C:\Users\you\OneDrive\Iron Owl backups"
            onChange={(e) => {
              setTypeDir(e.target.value);
              setTypeErr(null);
            }}
            aria-invalid={typeErr ? true : undefined}
          />
        </div>
        {typeErr && (
          <p className="st-alert" role="alert">
            {typeErr}
          </p>
        )}
        <DialogActions onCancel={() => setTypeDir(null)} submitLabel="Save" submitDisabled={!typeDir?.trim()} busy={saving} />
      </StDialog>
    </section>
  );

  async function saveTyped() {
    const d = typeDir?.trim();
    if (!d) return;
    setSaving(true);
    const err = await setDir(d);
    setSaving(false);
    if (err) setTypeErr(err);
    else setTypeDir(null);
  }
}

/** Features the design has no place for: the automatic backup files, and vaults set aside by restores. */
function MoreBackupOptions({ backup }: { backup: AutoBackup | undefined }) {
  const previous = useApi(() => api.previousVaults.list(), []);
  const files = backup?.dir ? backup.files : [];
  const prev = previous.data ?? [];
  return (
    <details className="st-more">
      <summary>More backup options</summary>
      <div className="st-more-body">
        <div>
          <h3>Automatic backups in the folder</h3>
          {!backup?.dir ? (
            <p className="st-help">Automatic backups are off.</p>
          ) : files.length === 0 ? (
            <p className="st-help">None yet. The first one is made the next time Iron Owl locks.</p>
          ) : (
            <ul className="ab-list">
              {files.map((f) => (
                <li key={f.name} className="ab-file">
                  <span className="ab-file-name">{f.name}</span>
                  <span className="small muted ab-file-meta">
                    {formatDateTime(f.created_at)} · {formatBytes(f.size_bytes)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <PreviousVaults items={prev} onChanged={previous.reload} />
      </div>
    </details>
  );
}

/**
 * Vaults set aside by earlier restores. Each still opens with the password it had, so after
 * restoring to get away from a compromised password, delete them here.
 */
function PreviousVaults({ items, onChanged }: { items: PreviousVault[]; onChanged: () => void }) {
  const say = useSay();
  const [target, setTarget] = useState<PreviousVault | null>(null);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    setTarget(null);
    setPassword('');
    setError(null);
  }

  async function remove() {
    if (!target) return;
    if (!password) return setError('Type your Iron Owl password.');
    setBusy(true);
    setError(null);
    try {
      await api.previousVaults.remove(target.id, password);
      say(`The copy set aside on ${formatDateTime(target.created_at)} was deleted.`);
      close();
      onChanged();
    } catch (e) {
      setPassword('');
      if (e instanceof ApiError && e.status === 401) setError('That isn’t your password now.');
      else if (e instanceof ApiError && e.status === 429) setError(`Too many tries. Please wait ${e.retryAfter ?? 30} seconds, then try again.`);
      else setError(plainErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Data set aside by a restore</h3>
      {items.length === 0 ? (
        <p className="st-help">Nothing is set aside.</p>
      ) : (
        <>
          <p className="st-help">
            When you restore a backup, what was here before is kept here. Each one still opens with the password it had then. Delete them once you’re sure you
            don’t need them.
          </p>
          <ul className="previous-list">
            {items.map((p) => (
              <li key={p.id} className="previous-row">
                <Icon name="lock" />
                <span className="previous-meta">
                  <span>Set aside {formatDateTime(p.created_at)}</span>
                  <span className="small muted">{formatBytes(p.size_bytes)} · encrypted</span>
                </span>
                <button type="button" className="st-btn st-btn-plain st-btn-sm" onClick={() => setTarget(p)}>
                  Delete…
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      <StDialog open={target !== null} size={480} title="Delete this copy?" onClose={close} onSubmit={() => void remove()} busy={busy}>
        <p className="st-dialog-lead">{target ? `The copy set aside on ${formatDateTime(target.created_at)} is deleted for good. This can’t be undone.` : ''}</p>
        <PasswordField
          label="Your Iron Owl password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            setError(null);
          }}
          disabled={busy}
          error={error}
          autoFocus
        />
        <div className="st-dialog-actions">
          <button type="button" className="st-btn st-btn-plain st-btn-lg" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="st-btn st-btn-danger st-btn-lg" disabled={busy}>
            {busy ? 'Deleting…' : 'Delete for good'}
          </button>
        </div>
      </StDialog>
    </div>
  );
}

// ---------------------------------------------------------------- Password and safety

const LOCK_LABEL: Record<AutoLockMinutes, string> = { 5: '5 minutes', 15: '15 minutes', 30: '30 minutes', 60: '1 hour' };

const SHEET_CHIP: Record<RecoveryStatus['status'], { tone: ChipTone; label: string }> = {
  active: { tone: 'ok', label: 'Ready' },
  none: { tone: 'warn', label: 'Not made yet' },
  unconfirmed: { tone: 'warn', label: 'Not finished' },
  stale: { tone: 'warn', label: 'Out of date' },
};

function sheetLine(st: RecoveryStatus): string {
  const made = sheetDate(st.created_at);
  switch (st.status) {
    case 'active':
      return `Sheet ${st.sheet ?? ''}${made ? ` · made ${made}` : ''}. Keep it with your important papers.`;
    case 'unconfirmed':
      return `Sheet ${st.sheet ?? ''} was made${made ? ` on ${made}` : ''}, but the last step wasn’t done. If you printed it or wrote it down, it works.`;
    case 'stale':
      return `Sheet ${st.sheet ?? ''} may not work any more. Make a new one so you can still get back in.`;
    default:
      return 'If you forget your password, this printed sheet gets you back in.';
  }
}

function passwordLine(at: string | null | undefined): string {
  if (!at) return 'The password you type to open Iron Owl.';
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return 'The password you type to open Iron Owl.';
  if (toISODate(d) === toISODate(new Date())) return 'Changed today.';
  return `Last changed ${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' })}.`;
}

function SafetyCard() {
  const { recovery, sheet, focus } = useSettings();
  const { lockNow, setAutoLockMinutes } = useApp();

  // Old deep link /settings?focus=recovery-sheet: bring the recovery sheet row into view.
  useEffect(() => {
    if (focus !== 'recovery-sheet') return;
    const h = window.setTimeout(() => document.getElementById('recovery-sheet')?.scrollIntoView({ block: 'center' }));
    return () => window.clearTimeout(h);
  }, [focus]);
  const fail = useFail();
  const uid = useId();
  const settings = useApi(() => api.settings.get(), []);
  const [lockBusy, setLockBusy] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const s = settings.data;
  const st = recovery.data;

  async function setLock(m: AutoLockMinutes) {
    if (!s || m === s.auto_lock_minutes) return;
    const prev = s;
    settings.setData({ ...s, auto_lock_minutes: m, auto_lock_source: 'vault' });
    setLockBusy(true);
    try {
      const next = await api.settings.update({ auto_lock_minutes: m });
      settings.setData(next);
      setAutoLockMinutes(next.auto_lock_minutes);
    } catch (e) {
      settings.setData(prev);
      fail('Couldn’t change when Iron Owl locks', e);
    } finally {
      setLockBusy(false);
    }
  }

  const chip = st ? SHEET_CHIP[st.status] : null;
  const hasSheet = st?.status === 'active';

  return (
    <section className="st-card" aria-labelledby={`${uid}-h`}>
      <div className="st-card-head">
        <div className="st-card-head-text">
          <h2 id={`${uid}-h`}>Password and safety</h2>
          <p className="st-desc">Keeps your money details private on this computer.</p>
        </div>
        <button type="button" className="st-btn st-btn-plain" onClick={() => void lockNow('manual')}>
          <Icon name="lock" />
          Lock now
        </button>
      </div>
      <div className="st-row is-stack">
        <span>
          <span className="st-label" id={`${uid}-lock`}>
            Lock Iron Owl when I step away for
          </span>
          <span className="st-help">You’ll type your password to open it again.</span>
        </span>
        {settings.error && !s ? (
          <p className="st-error is-inline">
            Iron Owl couldn’t load this setting.{' '}
            <button type="button" className="st-link st-link-inline" onClick={settings.reload}>
              Try again
            </button>
          </p>
        ) : (
          <StOptions
            label="Lock Iron Owl when I step away for"
            value={s && (AUTO_LOCK_CHOICES as number[]).includes(s.auto_lock_minutes) ? (s.auto_lock_minutes as AutoLockMinutes) : null}
            options={AUTO_LOCK_CHOICES.map((m) => ({ value: m, label: LOCK_LABEL[m] }))}
            onChange={(m) => void setLock(m)}
            disabled={!s || lockBusy}
          />
        )}
      </div>
      <div className="st-row">
        <span className="st-row-text">
          <span className="st-label">Password</span>
          <span className="st-help">{passwordLine(s?.password_changed_at)}</span>
        </span>
        <button type="button" className="st-btn st-btn-outline" onClick={() => setPwOpen(true)}>
          Change password…
        </button>
      </div>
      <div className="st-row is-last" id="recovery-sheet">
        <span className="st-row-text">
          <span className="st-sheet-title">
            <span className="st-label">Recovery sheet</span>
            {chip && <Chip tone={chip.tone}>{chip.label}</Chip>}
          </span>
          <span className="st-help">
            {st ? (
              sheetLine(st)
            ) : recovery.error ? (
              <>
                Iron Owl couldn’t check your recovery sheet.{' '}
                <button type="button" className="st-link st-link-inline" onClick={recovery.reload}>
                  Try again
                </button>
              </>
            ) : (
              ' '
            )}
          </span>
        </span>
        {st && (
          <button
            type="button"
            className={`st-btn ${hasSheet ? 'st-btn-plain' : 'st-btn-light'}`}
            onClick={st.status === 'unconfirmed' ? sheet.finish : sheet.make}
          >
            {st.status === 'active' || st.status === 'stale' ? 'Make a new sheet…' : st.status === 'unconfirmed' ? 'Finish the sheet…' : 'Make a recovery sheet'}
          </button>
        )}
      </div>
      <ChangePasswordDialog open={pwOpen} sheetActive={hasSheet} onClose={() => setPwOpen(false)} onChanged={settings.reload} />
    </section>
  );
}

function ChangePasswordDialog({ open, sheetActive, onClose, onChanged }: { open: boolean; sheetActive: boolean; onClose: () => void; onChanged: () => void }) {
  const say = useSay();
  const { lockNow } = useApp();
  const [cur, setCur] = useState('');
  const [n1, setN1] = useState('');
  const [n2, setN2] = useState('');
  const [busy, setBusy] = useState(false);
  const [wrong, setWrong] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const curRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setCur('');
    setN1('');
    setN2('');
    setWrong(null);
    setError(null);
  }, [open]);

  const long = [...n1].length >= MIN_PASSWORD_LENGTH;
  const match = n1.length > 0 && n1 === n2;
  const ok = long && match && cur.length > 0;

  async function save() {
    if (!ok || busy) return;
    if (n1 === cur) return setError('Pick a new password that’s different from the one you have now.');
    setBusy(true);
    setError(null);
    setWrong(null);
    try {
      const res = await api.auth.changePassword(cur, n1);
      onClose();
      onChanged();
      const backupNote = res.backup ? (res.backup.ok ? ' A fresh automatic backup was saved.' : ' The automatic backup after it didn’t work, so please save a backup now.') : '';
      say(`Password changed. Use the new one next time you open Iron Owl.${backupNote}`);
    } catch (e) {
      setCur('');
      if (e instanceof ApiError && e.status === 401) {
        setWrong(triesLine(triesLeft(e)));
        requestAnimationFrame(() => curRef.current?.focus());
      } else if (e instanceof ApiError && e.status === 429) setError(`Too many tries. Please wait ${e.retryAfter ?? 30} seconds, then try again.`);
      else setError(plainErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function goToSheet() {
    onClose();
    requestRecoverOnLock();
    void lockNow('manual');
  }

  return (
    <StDialog open={open} title="Change password" onClose={onClose} onSubmit={() => void save()} busy={busy}>
      <PasswordField
        label="Your password now"
        autoComplete="current-password"
        value={cur}
        onChange={(e) => {
          setCur(e.target.value);
          setWrong(null);
        }}
        inputRef={curRef}
        disabled={busy}
        autoFocus
        aria-invalid={wrong !== null || undefined}
      />
      {wrong !== null && (
        <p className="st-alert" role="alert">
          That isn’t your password now. {wrong ? `${wrong} ` : ''}Try again, or{' '}
          <button type="button" className="st-link st-link-inline" onClick={goToSheet}>
            use your recovery sheet
          </button>
          .
        </p>
      )}
      <PasswordField label="New password" autoComplete="new-password" value={n1} onChange={(e) => setN1(e.target.value)} hint={n1 ? <StrengthMeter password={n1} /> : undefined} disabled={busy} />
      <PasswordField label="Type the new password again" autoComplete="new-password" value={n2} onChange={(e) => setN2(e.target.value)} disabled={busy} />
      <ul className="st-checks" aria-live="polite">
        {[
          [long, `At least ${MIN_PASSWORD_LENGTH} characters. A few unrelated words works well.`],
          [match, 'Both new boxes match'],
        ].map(([done, label]) => (
          <li key={String(label)} className={done ? 'is-ok' : undefined}>
            <span className="st-check-mark" aria-hidden="true">
              {done ? '✓' : ''}
            </span>
            {label}
            <span className="sr-only">{done ? ' (done)' : ' (not yet)'}</span>
          </li>
        ))}
      </ul>
      <p className="st-help">
        {sheetActive ? 'Your recovery sheet keeps working. You don’t need to print a new one.' : 'Write the new password down and keep it somewhere safe.'}
      </p>
      {error && (
        <p className="st-alert is-plain" role="alert">
          {error}
        </p>
      )}
      <DialogActions onCancel={onClose} submitLabel={busy ? 'Saving…' : 'Save new password'} submitDisabled={!ok} busy={busy} />
    </StDialog>
  );
}

