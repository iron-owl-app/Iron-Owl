import { useEffect, useState } from 'react';
import type { CategoryState, Rule } from '../../api';

const TIMEOUT = 7000;

export interface UndoToast {
  /** New key = new toast (restarts the timer). */
  key: number;
  title: string;
  body: string;
  /** Categories to put back on Undo (empty when only a rule changed). */
  snapshot: CategoryState[];
  /** A rule this toast created ("Always for …"): Undo deletes it. */
  createdRuleId?: number;
  /** A rule this toast removed: Undo adds it back at `index`. */
  removedRule?: { rule: Rule; index: number };
  /** Offer "Always for {label}" (single merchant, no rule yet). */
  always: { field: 'merchant' | 'name'; text: string; category: string; label: string } | null;
  alwaysState: 'idle' | 'busy' | 'done';
}

/**
 * The page's own Undo toast (the app-wide toasts can't hold actions): bottom center,
 * one at a time, closes after 7 s unless hovered or focused.
 */
export function TxnUndoToast({ toast, onUndo, onAlways, onDismiss }: { toast: UndoToast; onUndo: () => void; onAlways: () => void; onDismiss: () => void }) {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (paused) return;
    const h = window.setTimeout(onDismiss, TIMEOUT);
    return () => window.clearTimeout(h);
  }, [toast.key, toast.alwaysState, paused, onDismiss]);

  return (
    <div
      className="txn-toast"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPaused(false);
      }}
    >
      <span className="txn-toast-text">
        <strong>{toast.title}</strong> {toast.body}
      </span>
      {toast.always && (
        <button type="button" className="txn-toast-rule" onClick={onAlways} disabled={toast.alwaysState !== 'idle'}>
          {toast.alwaysState === 'done' ? '✓ Rule added' : toast.alwaysState === 'busy' ? 'Adding rule…' : `Always for ${toast.always.label}`}
        </button>
      )}
      <button type="button" className="txn-toast-undo" onClick={onUndo} aria-keyshortcuts="U" title="Undo (U)">
        Undo
        <kbd className="txn-toast-key" aria-hidden="true">
          U
        </kbd>
      </button>
    </div>
  );
}
