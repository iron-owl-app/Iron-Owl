import { useCallback, useRef, useState } from 'react';
import type { UndoMessage } from '../../components/UndoToast';

/** One reversible change: `run` makes the inverse API calls (MAPPING §4.8). */
export interface UndoEntry {
  /** What was done, for "Undone: …". */
  label: string;
  run: () => Promise<void>;
}

const MAX = 20;

/**
 * The page's undo stack (at most 20, gone when leaving the page) and its message. The
 * toast shows the latest change; the toolbar Undo keeps working after the toast is gone.
 */
export function useUndoStack() {
  const stack = useRef<UndoEntry[]>([]);
  const [depth, setDepth] = useState(0);
  const [message, setMessage] = useState<UndoMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const keyRef = useRef(0);

  const show = useCallback((m: Omit<UndoMessage, 'key'>) => setMessage({ ...m, key: ++keyRef.current }), []);
  /** Update the current message in place (keeps its key, so its timer keeps running). */
  const patchMessage = useCallback((p: Partial<Omit<UndoMessage, 'key'>>) => setMessage((m) => (m ? { ...m, ...p } : m)), []);

  const push = useCallback(
    (entry: UndoEntry, m: Omit<UndoMessage, 'key'>) => {
      stack.current = [...stack.current, entry].slice(-MAX);
      setDepth(stack.current.length);
      show(m);
    },
    [show],
  );

  /**
   * Swap the newest entry (a single move that became "move all future ones"), but only while
   * the newest one still runs `expected`: something undone or done meanwhile must not be replaced.
   * Resolves whether it swapped.
   */
  const replaceTop = useCallback((entry: UndoEntry, expected: UndoEntry['run']): boolean => {
    const top = stack.current[stack.current.length - 1];
    if (!top || top.run !== expected) return false;
    stack.current = [...stack.current.slice(0, -1), entry];
    return true;
  }, []);

  /** Runs the newest entry. Resolves to the entry, or null when nothing ran / it failed (`onError`). */
  const undo = useCallback(
    async (onError: (e: unknown, entry: UndoEntry) => void): Promise<UndoEntry | null> => {
      const entry = stack.current[stack.current.length - 1];
      if (!entry || busy) return null;
      setBusy(true);
      try {
        await entry.run();
        stack.current = stack.current.slice(0, -1);
        setDepth(stack.current.length);
        show({ body: `Undone: ${entry.label}.`, undo: false });
        return entry;
      } catch (e) {
        // Something changed underneath (a sync, another tab): drop it rather than retry forever.
        stack.current = stack.current.slice(0, -1);
        setDepth(stack.current.length);
        setMessage(null);
        onError(e, entry);
        return null;
      } finally {
        setBusy(false);
      }
    },
    [busy, show],
  );

  const dismiss = useCallback(() => setMessage(null), []);

  return { depth, message, busy, push, replaceTop, undo, show, patchMessage, dismiss };
}
