import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from 'react';
import { Icon } from '../../components/Icon';
import './panels.css';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A right-hand panel (Find a bill or paycheck, Settings). Not a native modal <dialog> on purpose:
 * the page's Undo toast must stay clickable while it's open. It still acts like one: a scrim
 * that closes it, Esc, focus kept inside, and focus back where it was on close. Confirm and
 * edit dialogs opened from it sit on top (the browser's top layer).
 */
export function SidePanel({
  open,
  title,
  onClose,
  width,
  head,
  children,
  initialFocus,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  width: number;
  /** Extra sticky header content (the search box and filters). */
  head?: ReactNode;
  children: ReactNode;
  /** A selector inside the panel to focus on open (else the title). */
  initialFocus?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const latest = useRef(onClose);
  latest.current = onClose;

  useEffect(() => {
    if (!open) return;
    const back = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const el = ref.current;
    const first = (initialFocus && el?.querySelector<HTMLElement>(initialFocus)) || el?.querySelector<HTMLElement>('[data-panel-title]');
    first?.focus();
    return () => {
      if (back && back.isConnected) back.focus({ preventScroll: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      latest.current();
      return;
    }
    if (e.key !== 'Tab' || !ref.current) return;
    const list = Array.from(ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((x) => x.offsetParent !== null || x === document.activeElement);
    if (!list.length) return;
    const firstEl = list[0]!;
    const lastEl = list[list.length - 1]!;
    if (e.shiftKey && (document.activeElement === firstEl || !ref.current.contains(document.activeElement))) {
      e.preventDefault();
      lastEl.focus();
    } else if (!e.shiftKey && document.activeElement === lastEl) {
      e.preventDefault();
      firstEl.focus();
    }
  }

  return (
    <>
      <div className="calx-scrim" onClick={() => latest.current()} aria-hidden="true" />
      <div
        ref={ref}
        className="calx-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        style={{ width: `min(${width / 16}rem, 100vw)` }}
        onKeyDown={onKeyDown}
      >
        <div className="calx-panel-head">
          <div className="calx-panel-titlerow">
            <h2 id={titleId} tabIndex={-1} data-panel-title="">
              {title}
            </h2>
            <button type="button" className="icon-btn calx-close" onClick={() => latest.current()} aria-label="Close">
              <Icon name="x" />
            </button>
          </div>
          {head}
        </div>
        <div className="calx-panel-body">{children}</div>
      </div>
    </>
  );
}

/** A 44×26 on/off switch; the whole row is the button (a big target), the words are its name. */
export function Switch({
  checked,
  onChange,
  label,
  sub,
  disabled,
}: {
  checked: boolean;
  onChange: (on: boolean) => void;
  label: ReactNode;
  sub?: ReactNode;
  disabled?: boolean;
}) {
  const subId = useId();
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-describedby={sub ? subId : undefined}
      className="calx-switch-row"
      onClick={() => onChange(!checked)}
      disabled={disabled}
    >
      <span className={`cal-switch${checked ? ' is-on' : ''}`} aria-hidden="true" />
      <span className="calx-switch-text">
        <span className="calx-switch-label">{label}</span>
        {sub && (
          <span className="calx-switch-sub" id={subId}>
            {sub}
          </span>
        )}
      </span>
    </button>
  );
}
