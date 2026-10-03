import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import './shared-parts.css';

export interface PillOption<T extends string> {
  id: T;
  label: ReactNode;
  /** Spoken instead of `label` when the label is short or visual. */
  ariaLabel?: string;
}

/**
 * The beginner pill picker (Reports design: a pill container, radius 14, padding 4, with 44px
 * buttons; the selected one in the accent with dark text). A radio group by default; `role="tab"`
 * makes it a tab list (the Debt tab's Plan · Try it · Progress). Arrow keys move the choice, and
 * only the chosen pill is in the Tab order (roving tabindex).
 */
export function PillPicker<T extends string>({
  options,
  value,
  onChange,
  label,
  role = 'radio',
  idPrefix,
  className,
}: {
  options: PillOption<T>[];
  value: T;
  onChange: (id: T) => void;
  /** The group's name for screen readers. */
  label: string;
  role?: 'radio' | 'tab';
  /** With role="tab": the tabs get ids `${idPrefix}-tab-${id}` and control `${idPrefix}-panel-${id}`. */
  idPrefix?: string;
  className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const last = options.length - 1;
    let j = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = i === last ? 0 : i + 1;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = i === 0 ? last : i - 1;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = last;
    if (j < 0) return;
    e.preventDefault();
    onChange(options[j]!.id);
    refs.current[j]?.focus();
  };
  const isTab = role === 'tab';
  return (
    <div role={isTab ? 'tablist' : 'radiogroup'} aria-label={label} className={`pill-picker${className ? ` ${className}` : ''}`}>
      {options.map((o, i) => {
        const on = o.id === value;
        return (
          <button
            key={o.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role={role}
            aria-checked={isTab ? undefined : on}
            aria-selected={isTab ? on : undefined}
            id={isTab && idPrefix ? `${idPrefix}-tab-${o.id}` : undefined}
            aria-controls={isTab && idPrefix ? `${idPrefix}-panel-${o.id}` : undefined}
            aria-label={o.ariaLabel}
            tabIndex={on ? 0 : -1}
            className={`pill-picker-btn${on ? ' is-on' : ''}`}
            onClick={() => onChange(o.id)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
