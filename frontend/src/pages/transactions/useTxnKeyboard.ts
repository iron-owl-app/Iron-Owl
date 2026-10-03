import { useEffect, useRef } from 'react';

export type TxnKey = 'down' | 'up' | 'category' | 'suggest1' | 'suggest2' | 'tick' | 'undo' | 'escape';

const KEYS: Record<string, TxnKey> = {
  ArrowDown: 'down',
  j: 'down',
  ArrowUp: 'up',
  k: 'up',
  c: 'category',
  '1': 'suggest1',
  '2': 'suggest2',
  x: 'tick',
  u: 'undo',
  Escape: 'escape',
};

const NON_TEXT_INPUTS = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'image']);

/** Typing somewhere: text inputs, textareas, selects and contenteditable keep their keys. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return !NON_TEXT_INPUTS.has(target.type);
  return false;
}

/**
 * Transactions-page shortcuts on window: ↑↓/J/K move, C category, 1/2 accept a suggestion,
 * X tick, U undo (while the Undo toast shows), Esc close. Ignored with Ctrl/⌘/Alt held, while typing, while any modal <dialog> is
 * open, and while `paused` (the category picker handles its own keys). `onKey` returns true
 * when it used the key (the default action is then prevented).
 */
export function useTxnKeyboard(onKey: (key: TxnKey) => boolean, paused: boolean) {
  const handler = useRef(onKey);
  handler.current = onKey;
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    const listener = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || pausedRef.current) return;
      if (isTyping(e.target) || document.querySelector('dialog[open]')) return;
      const key = KEYS[e.key.length === 1 ? e.key.toLowerCase() : e.key];
      if (key && handler.current(key)) e.preventDefault();
    };
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, []);
}
