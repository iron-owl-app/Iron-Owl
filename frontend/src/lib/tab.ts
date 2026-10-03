/**
 * This window's id, sent as `X-FinTrack-Tab` on every request so the server can tell FinTrack's
 * windows apart ("already open in another window", close = lock, Open here).
 *
 * Not a secret and never an auth factor: it only names the window. Kept in sessionStorage so a
 * reload keeps the same id (the server then cancels the "window closed" lock for it). A
 * duplicated tab copies sessionStorage (this id and the session token); tabChannel's
 * `ensureOwnTabId` notices that at start and gives the copy an id of its own (`renewTabId`).
 */
const KEY = 'ft_tab_id';
const VALID = /^[A-Za-z0-9-]{8,64}$/;

let tabId: string | null = null;

function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function getTabId(): string {
  if (tabId) return tabId;
  try {
    const saved = sessionStorage.getItem(KEY);
    if (saved && VALID.test(saved)) tabId = saved;
  } catch {
    /* storage blocked: memory only */
  }
  if (!tabId) {
    tabId = newId();
    try {
      sessionStorage.setItem(KEY, tabId);
    } catch {
      /* storage blocked: memory only */
    }
  }
  return tabId;
}

/** A new id for this window (it turned out to be a duplicate of another window's). */
export function renewTabId(): string {
  tabId = newId();
  try {
    sessionStorage.setItem(KEY, tabId);
  } catch {
    /* storage blocked: memory only */
  }
  return tabId;
}
