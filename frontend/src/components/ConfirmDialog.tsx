import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Icon } from './Icon';

/**
 * Destructive-action confirmation on the native <dialog> element: real modal
 * semantics, focus containment, and Esc-to-cancel for free.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  cancelLabel = 'Cancel',
  busy,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  /** The safe choice (focused first). */
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      d.showModal();
      // Safer default focus for a destructive dialog.
      cancelRef.current?.focus();
    } else if (!open && d.open) {
      d.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onCancel();
      }}
      onClick={(e) => {
        // Click on the backdrop (the dialog element itself) cancels.
        if (e.target === ref.current && !busy) onCancel();
      }}
    >
      <div className="dialog-body">
        <span className="dialog-icon" aria-hidden="true">
          <Icon name="alert" />
        </span>
        <div>
          <h2 id={titleId}>{title}</h2>
          <div id={descId}>{children}</div>
        </div>
      </div>
      <div className="dialog-foot">
        <button ref={cancelRef} type="button" className="btn" onClick={onCancel} disabled={busy}>
          {cancelLabel}
        </button>
        <button type="button" className="btn btn-danger" onClick={onConfirm} disabled={busy}>
          {busy ? 'Removing…' : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
