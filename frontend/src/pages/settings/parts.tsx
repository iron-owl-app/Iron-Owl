import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { ApiError, errorMessage } from '../../api';
import { useToast } from '../../components/Toast';
import { UndoToast, type UndoMessage } from '../../components/UndoToast';

// ---------------------------------------------------------------- messages (bottom-center, with Undo)

export interface SayOptions {
  /** Shows Undo. Runs once; a failure is reported as an error toast. */
  undo?: () => Promise<unknown>;
}
type Say = (text: ReactNode, opts?: SayOptions) => void;

const SayContext = createContext<Say | null>(null);

/** The Settings page's one message at a time (the shared UndoToast: 12 s, paused on hover). */
export function useSay(): Say {
  const ctx = useContext(SayContext);
  if (!ctx) throw new Error('useSay outside SettingsMessages');
  return ctx;
}

export function SettingsMessages({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [msg, setMsg] = useState<(UndoMessage & { run?: () => Promise<unknown> }) | null>(null);
  const [busy, setBusy] = useState(false);
  const key = useRef(0);

  const say = useCallback<Say>((text, opts) => {
    key.current += 1;
    setBusy(false);
    setMsg({ key: key.current, body: text, undo: !!opts?.undo, run: opts?.undo });
  }, []);

  async function undo() {
    const m = msg;
    if (!m?.run || busy) return;
    setBusy(true);
    try {
      await m.run();
      setMsg(null);
    } catch (e) {
      setMsg(null);
      if (!(e instanceof ApiError && e.status === 401)) toast.push({ tone: 'error', title: 'Couldn’t undo that', body: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <SayContext.Provider value={say}>
      {children}
      <UndoToast message={msg} onUndo={msg?.run ? () => void undo() : undefined} undoBusy={busy} onClose={() => setMsg(null)} />
    </SayContext.Provider>
  );
}

/** Error toasts for failed saves (a 401 means FinTrack locked: the password screen takes over). */
export function useFail(): (title: string, e: unknown) => void {
  const toast = useToast();
  return useCallback(
    (title: string, e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return;
      toast.push({ tone: 'error', title, body: errorMessage(e) });
    },
    [toast],
  );
}

// ---------------------------------------------------------------- controls

/** The design's On/Off switch: a 48px button with a track and the word next to it. */
export function StSwitch({
  checked,
  onChange,
  label,
  onText = 'On',
  offText = 'Off',
  disabled,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  /** Accessible name (the row's label). */
  label: string;
  onText?: string;
  offText?: string;
  disabled?: boolean;
  describedBy?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      className="st-switch"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      onClick={() => onChange(!checked)}
      disabled={disabled}
    >
      <span className="st-switch-track" aria-hidden="true">
        <span className="st-switch-knob" />
      </span>
      <span aria-hidden="true">{checked ? onText : offText}</span>
    </button>
  );
}

export interface StOption<T> {
  value: T;
  label: string;
}

/** A radio group of 44px choices (arrow keys move the choice, like native radios). */
export function StOptions<T extends string | number>({
  label,
  value,
  options,
  onChange,
  disabled,
  grid,
}: {
  label: string;
  value: T | null;
  options: StOption<T>[];
  onChange: (v: T) => void;
  disabled?: boolean;
  grid?: boolean;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const current = options.findIndex((o) => o.value === value);
  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const last = options.length - 1;
    let j = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = i === last ? 0 : i + 1;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = i === 0 ? last : i - 1;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = last;
    if (j < 0) return;
    e.preventDefault();
    refs.current[j]?.focus();
    onChange(options[j]!.value);
  }
  return (
    <div role="radiogroup" aria-label={label} className={`st-options${grid ? ' is-grid' : ''}`}>
      {options.map((o, i) => (
        <button
          key={String(o.value)}
          ref={(el) => {
            refs.current[i] = el;
          }}
          type="button"
          role="radio"
          className="st-option"
          aria-checked={o.value === value}
          tabIndex={o.value === value || (current < 0 && i === 0) ? 0 : -1}
          onClick={() => onChange(o.value)}
          onKeyDown={(e) => onKey(e, i)}
          disabled={disabled}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export type ChipTone = 'ok' | 'warn' | 'bad' | 'plain';
export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`st-chip${tone === 'ok' ? '' : ` is-${tone}`}`}>{children}</span>;
}

// ---------------------------------------------------------------- dialog

/**
 * The design's window (520px, radius 18, padding 28, a 22px title): a native modal <dialog>,
 * so focus stays inside and Esc closes it. Clicking the dimmed page closes it too (not while
 * busy). `onSubmit` wraps the body in a form (Enter saves).
 */
export function StDialog({
  open,
  title,
  onClose,
  onSubmit,
  busy,
  size = 520,
  alert,
  children,
}: {
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  onSubmit?: () => void;
  busy?: boolean;
  size?: 480 | 520 | 540 | 660;
  /** role="alertdialog" (a question that needs an answer). */
  alert?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const latest = useRef({ open, busy, onClose });
  latest.current = { open, busy, onClose };

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    else if (!open && d.open) d.close();
  }, [open]);

  const body = (
    <>
      <h2 id={titleId}>{title}</h2>
      {children}
    </>
  );

  return (
    <dialog
      ref={ref}
      className={`st-dialog${size === 520 ? '' : ` is-${size}`}`}
      aria-labelledby={titleId}
      role={alert ? 'alertdialog' : undefined}
      onCancel={(e) => {
        e.preventDefault();
        if (!latest.current.busy) latest.current.onClose();
      }}
      onClose={() => {
        // The browser can close it on a repeated Esc: keep React's state in step.
        const { open: want, busy: b, onClose: close } = latest.current;
        const d = ref.current;
        if (!want || !d) return;
        if (!b) close();
        else if (!d.open) d.showModal();
      }}
      onClick={(e) => {
        if (e.target === ref.current && !latest.current.busy) latest.current.onClose();
      }}
    >
      {open &&
        (onSubmit ? (
          <form
            className="st-dialog-body"
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
              if (!busy) onSubmit();
            }}
          >
            {body}
          </form>
        ) : (
          <div className="st-dialog-body">{body}</div>
        ))}
    </dialog>
  );
}

/** Buttons row at the bottom of a StDialog: an optional left button (Delete), then Cancel and the main one. */
export function DialogActions({
  left,
  onCancel,
  cancelLabel = 'Cancel',
  submitLabel,
  submitDisabled,
  busy,
  submitType = 'submit',
  onSubmit,
}: {
  left?: ReactNode;
  onCancel: () => void;
  cancelLabel?: string;
  submitLabel: ReactNode;
  submitDisabled?: boolean;
  busy?: boolean;
  submitType?: 'submit' | 'button';
  onSubmit?: () => void;
}) {
  return (
    <div className="st-dialog-actions">
      {left}
      {left && <span className="st-spacer" />}
      <button type="button" className="st-btn st-btn-plain st-btn-lg" onClick={onCancel} disabled={busy}>
        {cancelLabel}
      </button>
      <button type={submitType} className="st-btn st-btn-primary st-btn-lg" disabled={submitDisabled || busy} onClick={onSubmit}>
        {submitLabel}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- small helpers

/** "$4,800" (cents only when there are some). */
export function dollars(n: number): string {
  const cents = Math.round(n * 100) % 100 !== 0;
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
}

/** "Sep 30" for a YYYY-MM-DD date (local, no time zone shift). */
export function monthDay(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  return new Date(y!, (m ?? 1) - 1, d ?? 1).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}
