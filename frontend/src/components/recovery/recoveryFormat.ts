/** "Oct 4, 2026" (short) or "October 4, 2026" (long) in the computer's own date style. */
export function sheetDate(iso: string | null | undefined, style: 'short' | 'long' = 'short'): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return new Intl.DateTimeFormat(undefined, { month: style, day: 'numeric', year: 'numeric' }).format(d);
}

/** "30 seconds", "1 second", "2 min 10 s", "15 min" — for "Please wait …". */
export function formatWait(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return `${s} second${s === 1 ? '' : 's'}`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return r ? `${m} min ${r} s` : `${m} min`;
}

/** "You have 3 tries left before a short wait." (null when the server didn't say, or none are left). */
export function triesLine(n: number | null): string | null {
  if (n === null || n <= 0) return null;
  return `You have ${n} ${n === 1 ? 'try' : 'tries'} left before a short wait.`;
}

/** Shorter form for a button: "29s", "2 min 10 s". */
export function formatWaitShort(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return s < 60 ? `${s}s` : formatWait(s);
}
