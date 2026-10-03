import { useEffect, useId, useRef, type ReactNode } from 'react';
import { Icon } from './Icon';
import { shouldCloseOnBackdrop } from './backdropClose';

/**
 * General modal on the native <dialog> element (focus containment, Esc to close).
 * Shared by the add-account flow, goal editor, rule editor, etc. Styles: `.modal*`
 * in styles.css. Width defaults to the design's 580px.
 */
export function Modal({
  open,
  title,
  subtitle,
  onClose,
  onBack,
  footer,
  children,
  width = 580,
  busy,
  dismissible = true,
  className,
}: {
  open: boolean;
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  /** Shows a ‹ back button in the header. */
  onBack?: () => void;
  footer?: ReactNode;
  children: ReactNode;
  width?: number;
  /** While busy, Esc / scrim / × don't close. */
  busy?: boolean;
  /**
   * false: Esc and clicks on the scrim do nothing and there's no × (the footer's buttons are
   * the only way out). For steps that would lose something if closed by accident.
   */
  dismissible?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const closable = dismissible && !busy;
  const latest = useRef({ open, closable, onClose });
  latest.current = { open, closable, onClose };

  // Backdrop clicks close only when pressed and released on the backdrop, and not right after
  // opening (see backdropClose.ts).
  const openedAt = useRef(0);
  const pressOnBackdrop = useRef(false);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      openedAt.current = performance.now();
      pressOnBackdrop.current = false;
      d.showModal();
    } else if (!open && d.open) d.close();
  }, [open]);

  // While it can't be closed, stop Esc before it becomes a close request. A cancelled Esc
  // keydown never reaches the dialog's close watcher, so no `cancel` and no `close` fire
  // (a repeated Esc can otherwise skip `cancel` and close it directly). Capture phase on the
  // document, so it holds wherever focus is; Esc meant for another dialog (e.g. a confirm
  // opened on top) is left alone.
  useEffect(() => {
    if (!open || closable) return;
    const onKeyDown = (e: KeyboardEvent) => {
      const d = ref.current;
      if (e.key !== 'Escape' || !d?.open) return;
      const owner = e.target instanceof Element ? e.target.closest('dialog') : null;
      if (owner === d) {
        e.preventDefault();
      } else if (owner === null) {
        // Focus outside any dialog: only if no other dialog is open that the Esc may be for.
        const others = Array.from(document.querySelectorAll('dialog[open]')).some((x) => x !== d);
        if (!others) e.preventDefault();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [open, closable]);

  return (
    <dialog
      ref={ref}
      className={className ? `modal ${className}` : 'modal'}
      style={{ maxWidth: `min(${width}px, calc(100vw - 32px))` }}
      aria-labelledby={titleId}
      onCancel={(e) => {
        e.preventDefault();
        if (closable) onClose();
      }}
      onClose={() => {
        // The browser can still close a dialog on a repeated Esc (close-watcher rules). Keep a
        // non-dismissible or busy one open; otherwise tell the owner so its state matches.
        const { open: wantOpen, closable: canClose, onClose: close } = latest.current;
        const d = ref.current;
        if (!wantOpen || !d) return;
        if (canClose) close();
        else if (!d.open) d.showModal();
      }}
      onPointerDown={(e) => {
        pressOnBackdrop.current = e.target === ref.current;
      }}
      onClick={(e) => {
        const close = shouldCloseOnBackdrop({
          pressOnBackdrop: pressOnBackdrop.current,
          releaseOnBackdrop: e.target === ref.current,
          openedAt: openedAt.current,
          now: performance.now(),
        });
        pressOnBackdrop.current = false;
        if (close && closable) onClose();
      }}
    >
      {open && (
        <>
          <div className="modal-head">
            {onBack && (
              <button type="button" className="icon-btn" onClick={onBack} disabled={busy} aria-label="Back">
                <Icon name="chevronLeft" />
              </button>
            )}
            <div className="modal-titles">
              <h2 id={titleId}>{title}</h2>
              {subtitle && <p>{subtitle}</p>}
            </div>
            {dismissible && (
              <button type="button" className="icon-btn" onClick={onClose} disabled={busy} aria-label="Close">
                <Icon name="x" />
              </button>
            )}
          </div>
          <div className="modal-body">{children}</div>
          {footer && <div className="modal-foot">{footer}</div>}
        </>
      )}
    </dialog>
  );
}
