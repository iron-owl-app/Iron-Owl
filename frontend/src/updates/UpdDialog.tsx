import { useEffect, useRef, type ReactNode, type RefObject } from 'react';

/**
 * The updates pop-ups (What's new, a picked file): a native modal <dialog> with no × in the
 * corner. Esc calls `onEscape` (Later / OK), or does nothing while `busy`. The scrim doesn't
 * close it: the buttons say what happens. `initialFocus`: the element to focus when it opens
 * (otherwise the browser picks the first button).
 */
export function UpdDialog({
  open,
  labelledBy,
  describedBy,
  role = 'dialog',
  className,
  onEscape,
  busy = false,
  initialFocus,
  children,
}: {
  open: boolean;
  labelledBy: string;
  describedBy?: string;
  role?: 'dialog' | 'alertdialog';
  className?: string;
  onEscape: () => void;
  busy?: boolean;
  initialFocus?: RefObject<HTMLElement>;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const latest = useRef({ open, busy, onEscape });
  latest.current = { open, busy, onEscape };

  // Focus goes back where it was (the banner or the Settings button) when it closes.
  const before = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      before.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      d.showModal();
      if (initialFocus?.current) initialFocus.current.focus();
    } else if (!open && d.open) {
      d.close();
      const b = before.current;
      before.current = null;
      if (b && document.contains(b)) b.focus({ preventScroll: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(
    () => () => {
      if (ref.current?.open) ref.current.close();
    },
    [],
  );

  return (
    <dialog
      ref={ref}
      role={role === 'alertdialog' ? 'alertdialog' : undefined}
      className={`upd-dialog${className ? ` ${className}` : ''}`}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      aria-modal="true"
      onCancel={(e) => {
        e.preventDefault();
        if (!latest.current.busy) latest.current.onEscape();
      }}
      onClose={() => {
        // A repeated Esc can close a dialog without `cancel`: keep it open while it should be.
        const d = ref.current;
        if (latest.current.open && d && !d.open) {
          if (latest.current.busy) d.showModal();
          else latest.current.onEscape();
        }
      }}
    >
      {open && children}
    </dialog>
  );
}
