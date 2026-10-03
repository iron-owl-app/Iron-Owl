/**
 * "Use your recovery sheet" from Settings › Change password: FinTrack locks, and the password
 * screen then opens straight on the recovery sheet steps. A one-time flag in this window's
 * sessionStorage (never anything secret).
 */
const KEY = 'fintrack.openRecover';

export function requestRecoverOnLock(): void {
  try {
    window.sessionStorage.setItem(KEY, '1');
  } catch {
    /* storage blocked: the password screen opens as usual */
  }
}

/** Read in a state initializer (may run twice in development), so it doesn't clear the flag. */
export function hasRecoverRequest(): boolean {
  try {
    return window.sessionStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

export function clearRecoverRequest(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}
