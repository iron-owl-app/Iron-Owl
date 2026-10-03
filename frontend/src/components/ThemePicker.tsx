import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useTheme } from '../lib/theme';
import { popoverPlace, THEMES, themeLabel, type ThemeChoice, type ThemeOption } from '../lib/themeCore';
import { Icon } from './Icon';

/**
 * Colors (Release 3.16): Match Windows / Light / Dark / Warm night as one radio group
 * (arrow keys move and pick, like the Text size choice). Used by the sidebar's Colors button,
 * the phone Menu sheet and Settings › App; all read the same saved choice (lib/theme.ts).
 */
export function ThemeRadios({
  value,
  onPick,
  className,
  itemClassName,
  labelledBy,
  label,
  renderItem,
}: {
  value: ThemeChoice;
  onPick: (choice: ThemeChoice) => void;
  className: string;
  itemClassName: string;
  labelledBy?: string;
  label?: string;
  renderItem: (t: ThemeOption, checked: boolean) => ReactNode;
}) {
  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const n = THEMES.length;
    const j = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? (i + 1) % n : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? (i + n - 1) % n : -1;
    if (j < 0) return;
    e.preventDefault();
    onPick(THEMES[j]!.id);
    (e.currentTarget.parentElement?.children[j] as HTMLElement | undefined)?.focus();
  }
  return (
    <div role="radiogroup" aria-labelledby={labelledBy} aria-label={labelledBy ? undefined : label} className={className}>
      {THEMES.map((t, i) => (
        <button
          key={t.id}
          type="button"
          role="radio"
          className={itemClassName}
          aria-checked={value === t.id}
          tabIndex={value === t.id ? 0 : -1}
          onClick={() => onPick(t.id)}
          onKeyDown={(e) => onKey(e, i)}
        >
          {renderItem(t, value === t.id)}
        </button>
      ))}
    </div>
  );
}

/** A row's check mark (sidebar and Menu sheet): shown on the picked theme only. */
function Tick({ on }: { on: boolean }) {
  return <Icon name="check" className={`theme-tick${on ? '' : ' is-off'}`} />;
}

/**
 * The sidebar's Colors button (bottom of the sidebar). Its four choices open in a small panel
 * to the right of the sidebar, bottom-aligned with the button and kept inside the window, so
 * the button never moves (popoverPlace). Focus goes to the picked choice; arrows move and pick
 * (the new colors show at once, the panel stays open to compare); Esc, a click outside or
 * tabbing away closes it.
 */
export function ColorsButton() {
  const [theme, setTheme] = useTheme();
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const uid = useId();

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    setPos(null);
    if (refocus) btn.current?.focus();
  }, []);

  // Place it once it has a size, then put focus on the picked choice.
  useLayoutEffect(() => {
    if (!open || !btn.current || !pop.current) return;
    const place = () => {
      if (!btn.current || !pop.current) return;
      const b = btn.current.getBoundingClientRect();
      const p = pop.current.getBoundingClientRect();
      setPos(popoverPlace(b, { width: p.width, height: p.height }, { width: window.innerWidth, height: window.innerHeight }));
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    pop.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus({ preventScroll: true });
    const outside = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!pop.current?.contains(t) && !btn.current?.contains(t)) close(false);
    };
    document.addEventListener('pointerdown', outside, true);
    return () => document.removeEventListener('pointerdown', outside, true);
  }, [open, close]);

  return (
    <>
      <button
        ref={btn}
        type="button"
        className="side-action"
        aria-expanded={open}
        aria-controls={open ? `${uid}-pop` : undefined}
        aria-haspopup="true"
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        <Icon name="theme" />
        <span id={`${uid}-h`}>Colors</span>
        <span className="side-action-value">{themeLabel(theme)}</span>
      </button>
      {open &&
        createPortal(
          <div
            ref={pop}
            id={`${uid}-pop`}
            className="colors-pop"
            style={pos ? { left: pos.left, top: pos.top } : { visibility: 'hidden', left: 0, top: 0 }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.stopPropagation();
                close(true);
              }
            }}
            onBlur={(e) => {
              const next = e.relatedTarget as Node | null;
              if (next && !pop.current?.contains(next) && !btn.current?.contains(next)) close(false);
            }}
          >
            <p className="colors-pop-h" aria-hidden="true">
              Colors
            </p>
            <ThemeRadios
              value={theme}
              onPick={setTheme}
              labelledBy={`${uid}-h`}
              className="side-colors-list"
              itemClassName="side-colors-item"
              renderItem={(t, on) => (
                <>
                  <ThemeDot id={t.id} />
                  <span className="side-colors-label">{t.label}</span>
                  <Tick on={on} />
                </>
              )}
            />
          </div>,
          document.body,
        )}
    </>
  );
}

/** The phone Menu sheet's Colors rows (48px, like the other rows there). */
export function MenuColors() {
  const [theme, setTheme] = useTheme();
  const uid = useId();
  return (
    <section className="menu-colors" aria-labelledby={`${uid}-h`}>
      <h3 id={`${uid}-h`} className="menu-colors-h">
        Colors
      </h3>
      <ThemeRadios
        value={theme}
        onPick={setTheme}
        labelledBy={`${uid}-h`}
        className="menu-list"
        itemClassName="menu-item"
        renderItem={(t, on) => (
          <>
            <ThemeDot id={t.id} />
            <span className="menu-label">{t.label}</span>
            <Tick on={on} />
          </>
        )}
      />
    </section>
  );
}

/** A small sample of each theme's page and accent colors (fixed colors: it shows the theme you'd get). */
export function ThemeDot({ id, big = false }: { id: ThemeChoice; big?: boolean }) {
  return <span className={`theme-dot theme-dot-${id}${big ? ' is-big' : ''}`} aria-hidden="true" />;
}
