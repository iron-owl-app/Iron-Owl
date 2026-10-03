import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from './Icon';
import { filterMonths, monthOptionLabel } from '../lib/monthPickerMath';
import './MonthPicker.css';

/**
 * Pick one month: ‹ and › step a month, and the month's name opens a list of every month (newest
 * first) with a search box on top (combobox pattern: type "march" or "2025", arrows move, Enter
 * picks, Escape closes). Used by Reports › Overview (One month) and Reports › Year over year.
 */
export function MonthPicker({
  months,
  value,
  onChange,
  thisMonth,
  label = 'Month',
}: {
  /** 'YYYY-MM', oldest first. */
  months: string[];
  value: string;
  onChange: (month: string) => void;
  /** Today's month: its name gets "(this month)". */
  thisMonth: string;
  /** The picker's name for screen readers. */
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const listId = useId();
  const optId = useId();

  const at = months.indexOf(value);
  const newestFirst = [...months].reverse();
  const shown = filterMonths(newestFirst, query, thisMonth);

  const close = (focusButton: boolean) => {
    setOpen(false);
    setQuery('');
    if (focusButton) buttonRef.current?.focus();
  };
  const pick = (m: string) => {
    onChange(m);
    close(true);
  };
  const openList = () => {
    setQuery('');
    setActive(Math.max(0, newestFirst.indexOf(value)));
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) close(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  // Keep the highlighted month in view.
  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [open, active]);

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => Math.min(shown.length - 1, i + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === 'Home' && !query) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End' && !query) {
      e.preventDefault();
      setActive(shown.length - 1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const m = shown[active];
      if (m) pick(m);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      close(true);
    } else if (e.key === 'Tab') {
      close(false);
    }
  };

  return (
    <div className="mpk" ref={rootRef} role="group" aria-label={label}>
      <button type="button" className="mpk-step" aria-label="Previous month" disabled={at <= 0} onClick={() => onChange(months[at - 1]!)}>
        <Icon name="chevronLeft" />
      </button>
      <button
        ref={buttonRef}
        type="button"
        className="mpk-open"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${monthOptionLabel(value, thisMonth)}. Choose another month`}
        onClick={() => (open ? close(false) : openList())}
      >
        <strong aria-live="polite">{monthOptionLabel(value, thisMonth)}</strong>
        <Icon name="chevronDown" className="mpk-caret" />
      </button>
      <button
        type="button"
        className="mpk-step"
        aria-label="Next month"
        disabled={at < 0 || at >= months.length - 1}
        onClick={() => onChange(months[at + 1]!)}
      >
        <Icon name="chevronRight" />
      </button>
      {open && (
        <div className="mpk-pop">
          <input
            ref={inputRef}
            className="mpk-search"
            type="text"
            role="combobox"
            aria-label="Find a month"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={shown[active] ? `${optId}-${active}` : undefined}
            placeholder="Find a month, e.g. March 2025"
            autoComplete="off"
            spellCheck={false}
            maxLength={40}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onKey}
          />
          <ul ref={listRef} id={listId} className="mpk-list" role="listbox" aria-label="Months">
            {shown.map((m, i) => (
              <li
                key={m}
                id={`${optId}-${i}`}
                data-i={i}
                role="option"
                aria-selected={m === value}
                className={`mpk-opt${i === active ? ' is-active' : ''}${m === value ? ' is-picked' : ''}`}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(m)}
              >
                {monthOptionLabel(m, thisMonth)}
              </li>
            ))}
          </ul>
          {!shown.length && (
            <p className="mpk-none" role="status">
              No month matches “{query.trim()}”. Try a month name or a year.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
