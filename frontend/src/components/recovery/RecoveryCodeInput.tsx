import { useEffect, useId, useRef, type ClipboardEvent, type KeyboardEvent } from 'react';
import { GROUPS, GROUP_LEN, badGroups, cleanDigits, distribute, groupOk, typoMessage } from '../../lib/recoveryCode';
import { Icon } from '../Icon';
import './recovery.css';

/**
 * Six big boxes for the 6 groups on a recovery sheet (design screen 4). Moves to the next box
 * after 6 digits, Backspace in an empty box goes back, arrows cross box edges, and pasting 7+
 * digits fills this box and the ones after it (exactly 36 fill from Group 1). A complete group
 * shows ✓ or "Check this group" from its check digit, and typos are described in one polite
 * status line. The boxes have no `name` and autocomplete is off, so browsers don't save them;
 * the digits live only in the caller's state.
 */
export function RecoveryCodeInput({
  value,
  onChange,
  disabled,
  describedBy,
  autoFocus,
}: {
  value: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** Extra ids for aria-describedby (the caller's progress line). */
  describedBy?: string;
  autoFocus?: boolean;
}) {
  const uid = useId();
  const refs = useRef<(HTMLInputElement | null)[]>([]);
  const statusId = `${uid}-status`;
  const typos = badGroups(value);

  // A box to focus once the new values are on screen (set together with onChange).
  const pendingFocus = useRef<{ i: number; caret: 'start' | 'end' } | null>(null);
  useEffect(() => {
    const p = pendingFocus.current;
    if (!p) return;
    pendingFocus.current = null;
    focusNow(p.i, p.caret);
  });

  function focusNow(i: number, caret: 'start' | 'end') {
    const el = refs.current[i];
    if (!el) return;
    el.focus();
    const at = caret === 'start' ? 0 : el.value.length;
    try {
      el.setSelectionRange(at, at);
    } catch {
      /* some input types don't support selection */
    }
  }

  /** Focus box `i` after the change the caller is about to make has rendered. */
  function focusBox(i: number, caret: 'start' | 'end') {
    pendingFocus.current = { i, caret };
  }

  function set(i: number, v: string) {
    const next = [...value];
    while (next.length < GROUPS) next.push('');
    next[i] = v;
    onChange(next);
  }

  function handleInput(i: number, raw: string) {
    const digits = cleanDigits(raw);
    const prev = value[i] ?? '';
    if (digits.length <= GROUP_LEN) {
      set(i, digits);
      if (digits.length === GROUP_LEN && prev.length < GROUP_LEN && i < GROUPS - 1) focusBox(i + 1, 'end');
      return;
    }
    if (digits.length === GROUP_LEN + 1 && prev.length === GROUP_LEN) {
      // A 7th digit typed into a full box: carry it on into an empty next box, else ignore it.
      if (digits.startsWith(prev) && i < GROUPS - 1 && !value[i + 1]) {
        const next = [...value];
        next[i + 1] = digits.slice(GROUP_LEN);
        onChange(next);
        focusBox(i + 1, 'end');
      }
      return;
    }
    // Pasted or auto-filled without a paste event.
    const r = distribute(value, i, digits);
    onChange(r.boxes);
    focusBox(r.focus, 'end');
  }

  function handlePaste(i: number, e: ClipboardEvent<HTMLInputElement>) {
    const digits = cleanDigits(e.clipboardData.getData('text'));
    if (digits.length <= GROUP_LEN) return; // a normal paste into one box: onChange cleans it
    e.preventDefault();
    const r = distribute(value, i, digits);
    onChange(r.boxes);
    focusBox(r.focus, 'end');
  }

  function handleKey(i: number, e: KeyboardEvent<HTMLInputElement>) {
    const el = e.currentTarget;
    const atStart = el.selectionStart === 0 && el.selectionEnd === 0;
    const atEnd = el.selectionStart === el.value.length && el.selectionEnd === el.value.length;
    if (e.key === 'Backspace' && el.value === '' && i > 0) {
      e.preventDefault();
      focusNow(i - 1, 'end');
    } else if (e.key === 'ArrowLeft' && atStart && i > 0) {
      e.preventDefault();
      focusNow(i - 1, 'end');
    } else if (e.key === 'ArrowRight' && atEnd && i < GROUPS - 1) {
      e.preventDefault();
      focusNow(i + 1, 'start');
    }
  }

  return (
    <fieldset className="rc-fieldset" disabled={disabled}>
      <legend className="sr-only">The 6 groups of numbers from your recovery sheet</legend>
      <div className="rc-grid">
        {Array.from({ length: GROUPS }, (_, i) => {
          const g = value[i] ?? '';
          const full = g.length === GROUP_LEN;
          const ok = full && groupOk(i + 1, g);
          const state = !full ? '' : ok ? ' is-ok' : ' is-typo';
          const labelId = `${uid}-l${i}`;
          return (
            <div className={`rc-box${state}`} key={i}>
              <label className="rc-label" htmlFor={`${uid}-g${i}`}>
                <span id={labelId}>Group {i + 1}</span>
                {full && (
                  <span className="rc-mark" aria-hidden="true">
                    {ok ? '✓' : 'Check this group'}
                  </span>
                )}
              </label>
              <input
                ref={(el) => {
                  refs.current[i] = el;
                }}
                id={`${uid}-g${i}`}
                className="rc-input"
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                data-lpignore="true"
                data-1p-ignore="true"
                aria-labelledby={labelId}
                aria-invalid={full && !ok ? true : undefined}
                aria-describedby={[statusId, describedBy].filter(Boolean).join(' ')}
                value={g}
                autoFocus={autoFocus && i === 0}
                onChange={(e) => handleInput(i, e.target.value)}
                onPaste={(e) => handlePaste(i, e)}
                onKeyDown={(e) => handleKey(i, e)}
              />
            </div>
          );
        })}
      </div>
      <div id={statusId} role="status" className="rs-live">
        {typos.length > 0 && (
          <div className="rs-note rs-note-amber">
            <span className="rs-note-ic" aria-hidden="true">
              <Icon name="alert" />
            </span>
            <div>
              <div className="rs-note-title">{typoMessage(typos)}</div>
              <div className="rs-note-body">
                {typos.length === 1
                  ? 'Check it against your sheet. One of its numbers is probably different.'
                  : 'Check them against your sheet. One of the numbers in each is probably different.'}
              </div>
            </div>
          </div>
        )}
      </div>
    </fieldset>
  );
}
