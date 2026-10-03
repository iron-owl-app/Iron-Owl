import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { formatMoney } from './format';
import { moneyDraftDecision } from './moneyDraft';

/**
 * The low-balance limit's typing and saving, shared by Bills and paychecks › Settings and Settings › Alerts (and
 * its Large purchase amount): whole dollars, saved on blur or Enter, Escape puts it back.
 * `onSave` resolves true when saved.
 */
export function useMoneyDraft(value: number, onSave: (v: number) => Promise<boolean>) {
  const fmt = (v: number) => formatMoney(v, { cents: false }).replace('$', '');
  const [draft, setDraft] = useState(() => fmt(value));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const focused = useRef(false);
  /** Set by Escape: the blur that follows puts the saved value back instead of saving. */
  const cancelled = useRef(false);
  useEffect(() => {
    if (!focused.current) setDraft(fmt(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  useEffect(() => {
    if (!saved) return;
    const h = window.setTimeout(() => setSaved(false), 2000);
    return () => window.clearTimeout(h);
  }, [saved]);

  async function commit() {
    focused.current = false;
    const d = moneyDraftDecision(draft, value, cancelled.current);
    cancelled.current = false;
    if (d.kind === 'reset') {
      setDraft(fmt(value));
      setError(null);
      return;
    }
    if (d.kind === 'error') {
      setError(d.message);
      setDraft(fmt(value));
      return;
    }
    setError(null);
    setDraft(fmt(d.value));
    if (d.kind === 'same') return;
    if (await onSave(d.value)) setSaved(true);
    else setDraft(fmt(value));
  }
  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.currentTarget.blur();
    } else if (e.key === 'Escape') {
      // Only the typing is undone: don't also close a panel or dialog around the box.
      e.stopPropagation();
      cancelled.current = true;
      setDraft(fmt(value));
      setError(null);
      e.currentTarget.blur();
    }
  }
  return {
    error,
    saved,
    inputProps: {
      inputMode: 'numeric' as const,
      value: draft,
      autoComplete: 'off',
      onChange: (e: { target: { value: string } }) => {
        setDraft(e.target.value);
        setError(null);
      },
      onFocus: () => {
        focused.current = true;
      },
      onBlur: () => void commit(),
      onKeyDown,
      'aria-invalid': error ? true : undefined,
    },
  };
}
