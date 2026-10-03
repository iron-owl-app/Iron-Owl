import { useEffect, useRef, useState, type ReactNode } from 'react';

/** How long a message stays: long enough to read and reach Undo; paused while hovered or focused. */
export const UNDO_TOAST_MS = 12_000;

/** One optional extra button before Undo ("Move all future ones too"). */
export interface UndoAction {
  label: string;
  run: () => void;
  /** Shows "Working…" and holds the timer. */
  busy?: boolean;
  /** Once done, the button stays (disabled) with this label (default "✓ {label}"). */
  done?: boolean;
  doneLabel?: string;
}

export interface UndoMessage {
  /** A new key is a new message (restarts the timer). */
  key: number;
  /** Bold first words. */
  title?: ReactNode;
  /** Plain text after the title (or the whole message). */
  body?: ReactNode;
  action?: UndoAction;
  /** false hides the Undo button (an Undo that already ran, a message with nothing to undo). Default true. */
  undo?: boolean;
}

export interface UndoToastProps {
  /** null shows nothing; the live region stays mounted so each new message is announced. */
  message: UndoMessage | null;
  /** Shows Undo (unless `message.undo === false`). The caller decides what happens to the message next. */
  onUndo?: () => void;
  /** Shows "Undoing…" and holds the timer. */
  undoBusy?: boolean;
  /** The Close button and the timer. */
  onClose: () => void;
  /** Default 12 s; 0 keeps the message until it's closed. */
  timeout?: number;
}

/**
 * The shared Undo message, bottom center, one at a time (the app-wide toasts can't hold
 * actions): Budget and Bills and paychecks use it (Transactions keeps its own
 * TxnUndoToast). It closes after 12 seconds unless the pointer or keyboard focus is on it or
 * something is running, so a slow reader never loses the Undo. The `role="status"` region is
 * always mounted so screen readers announce each new message. Colors: the shared `--toast-*`
 * tokens (an inverted surface in both themes).
 */
export function UndoToast({ message, onUndo, undoBusy = false, onClose, timeout = UNDO_TOAST_MS }: UndoToastProps) {
  return (
    <div className="undo-toast-region" role="status" aria-live="polite">
      {message && <UndoToastCard key={message.key} message={message} onUndo={onUndo} undoBusy={undoBusy} onClose={onClose} timeout={timeout} />}
    </div>
  );
}

function UndoToastCard({
  message,
  onUndo,
  undoBusy,
  onClose,
  timeout,
}: {
  message: UndoMessage;
  onUndo?: () => void;
  undoBusy: boolean;
  onClose: () => void;
  timeout: number;
}) {
  const [paused, setPaused] = useState(false);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const a = message.action;
  const busy = undoBusy || !!a?.busy;

  useEffect(() => {
    if (paused || busy || !timeout) return;
    const h = window.setTimeout(() => closeRef.current(), timeout);
    return () => window.clearTimeout(h);
  }, [paused, busy, timeout, a?.done]);

  const hasTitle = message.title != null && message.title !== '';
  return (
    <div
      className="undo-toast"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPaused(false);
      }}
    >
      <span className="undo-toast-text">
        {hasTitle && <strong>{message.title}</strong>}
        {hasTitle && message.body ? ' ' : null}
        {message.body}
      </span>
      <span className="undo-toast-actions">
        {a && (
          <button type="button" className="undo-toast-btn is-action" onClick={a.run} disabled={a.busy || a.done}>
            {a.done ? (a.doneLabel ?? `✓ ${a.label}`) : a.busy ? 'Working…' : a.label}
          </button>
        )}
        {onUndo && message.undo !== false && (
          <button type="button" className="undo-toast-btn is-undo" onClick={onUndo} disabled={busy}>
            {undoBusy ? 'Undoing…' : 'Undo'}
          </button>
        )}
        <button type="button" className="undo-toast-btn is-close" onClick={onClose}>
          Close
        </button>
      </span>
    </div>
  );
}
