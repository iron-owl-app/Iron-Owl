/**
 * UI preferences only (chart range, "show hidden" toggles, page size).
 * Nothing sensitive is ever stored here. Every access is wrapped in try/catch
 * because storage can be unavailable (private windows, blocked site data).
 */
const PREFIX = 'fintrack.ui.';

export function readPref<T>(key: string, fallback: T, isValid?: (v: unknown) => v is T): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    const parsed: unknown = JSON.parse(raw);
    if (isValid ? isValid(parsed) : typeof parsed === typeof fallback) return parsed as T;
  } catch {
    /* ignore */
  }
  return fallback;
}

export function removePref(key: string): void {
  try {
    window.localStorage.removeItem(PREFIX + key);
  } catch {
    /* ignore */
  }
}

export function writePref<T>(key: string, value: T): void {
  try {
    window.localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}
