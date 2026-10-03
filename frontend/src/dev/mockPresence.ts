/**
 * DEV-ONLY: the mock server's sessions and windows (MAPPING "Already open" model + API), shared
 * by every mock tab on this origin through localStorage, so two tabs of the mock behave like two
 * FinTrack windows on one server: one holds the session, the other sees "already open".
 *
 *   ?mock=elsewhere    another (pretend) window holds the session: "already open" (no one answers
 *                      Open here, so it falls back to the password after 1.5 s)
 *   ?mock=idle|closed  locked, with that reason       ?mock=locked   locked, fresh start (Sign in)
 *   ?mock=offline      nothing answers (FinTrack isn't running, FT-START-02)
 *   &down=20           stop answering 20 s after the page loads (FT-RUN-01); &up=60 answer again
 *   &reset=1           forget the shared sessions first
 *
 * Two real tabs: open `?mock=full` in one, then the same URL in another tab → "already open";
 * Open here hands the session over (BroadcastChannel) and the first tab shows "moved".
 * With ?mock=full a tab gets the session by itself when no other live window holds it.
 */
const KEY = 'fintrack.mock.server';
const TTL = 100_000;
const CLOSE_GRACE = 8_000;
/** The pretend window in ?mock=elsewhere (always "seen"; it never answers Open here). */
export const OTHER_WINDOW = 'mock-other-window';

type Reason = 'idle' | 'closed' | 'manual' | 'moved' | 'shutdown';
interface Server {
  token: string | null;
  boundTab: string | null;
  seen: Record<string, number>;
  lockReason: Reason | null;
  moved: string[];
  pendingClose: { tab: string; at: number } | null;
  pretendOther: boolean;
}

const fresh = (): Server => ({ token: null, boundTab: null, seen: {}, lockReason: null, moved: [], pendingClose: null, pretendOther: false });

function load(): Server {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) return { ...fresh(), ...(JSON.parse(raw) as Partial<Server>) };
  } catch {
    /* ignore */
  }
  return fresh();
}

function save(s: Server) {
  // At most 32 windows remembered, like the server.
  const tabs = Object.entries(s.seen).sort((a, b) => b[1] - a[1]).slice(0, 32);
  s.seen = Object.fromEntries(tabs);
  try {
    window.localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* ignore */
  }
}

const newToken = () => `mock-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;

/** Heard from within the TTL and not closing (like presence.py `present`). */
function present(s: Server, tab: string | null): boolean {
  if (!tab) return false;
  if (s.pendingClose?.tab === tab) return false;
  return Date.now() - (s.seen[tab] ?? 0) <= TTL;
}

/** Lazy server clock: a window that said "closing" and didn't come back locks FinTrack. */
function tick(s: Server) {
  if (s.pendingClose && Date.now() >= s.pendingClose.at) {
    if (s.pendingClose.tab === s.boundTab) lockServer(s, 'closed');
    s.pendingClose = null;
  }
  if (s.pretendOther) s.seen[OTHER_WINDOW] = Date.now();
}

function lockServer(s: Server, reason: Reason) {
  s.token = null;
  s.boundTab = null;
  s.moved = [];
  s.lockReason = reason;
  s.pretendOther = false;
}

// ---------------------------------------------------------------- scenario setup

let downAt = 0;
let upAt = 0;

export function initPresence(scenario: string, qs: URLSearchParams) {
  const reset = qs.get('reset') === '1';
  const down = Number(qs.get('down') ?? 0);
  const up = Number(qs.get('up') ?? 0);
  if (scenario === 'offline') downAt = 1;
  else if (down > 0) downAt = Date.now() + down * 1000;
  if (up > 0) upAt = Date.now() + up * 1000;

  let s = reset ? fresh() : load();
  if (scenario === 'elsewhere') {
    s = fresh();
    s.token = newToken();
    s.boundTab = OTHER_WINDOW;
    s.pretendOther = true;
    s.seen[OTHER_WINDOW] = Date.now();
  } else if (scenario === 'idle' || scenario === 'closed' || scenario === 'locked' || scenario === 'lockout' || scenario === 'setup') {
    s = fresh();
    s.lockReason = scenario === 'idle' ? 'idle' : scenario === 'closed' ? 'closed' : null;
  }
  save(s);
}

/** Whether the pretend server is down right now (requests then fail like a network error). */
export function serverDown(): boolean {
  const now = Date.now();
  if (upAt && now >= upAt) return false;
  return downAt > 0 && now >= downAt;
}

// ---------------------------------------------------------------- requests

export type SessionState = 'ok' | 'locked' | 'moved';

/** Every request: mark the window seen (status and alive also cancel its pending close). */
export function seen(tab: string | null, cancelsClose: boolean) {
  const s = load();
  tick(s);
  if (tab) {
    s.seen[tab] = Date.now();
    if (cancelsClose && s.pendingClose?.tab === tab) s.pendingClose = null;
  }
  save(s);
}

export function sessionState(token: string | null): SessionState {
  const s = load();
  tick(s);
  save(s);
  if (token && s.token && token === s.token) return 'ok';
  if (token && s.moved.includes(token)) return 'moved';
  return 'locked';
}

/** The server's view for this window: what status and alive add. */
export function presenceFor(tab: string | null, token: string | null) {
  const s = load();
  tick(s);
  save(s);
  const valid = !!(token && s.token && token === s.token);
  const boundSeen = present(s, s.boundTab);
  return {
    unlocked: valid,
    open_elsewhere: !!s.token && !!s.boundTab && s.boundTab !== tab && boundSeen && !valid,
    lock_reason: valid ? null : s.lockReason,
  };
}

/** Would a new tab with `ftMockAutoSession` get the session by itself? (?mock=full and friends) */
export function canAutoAdopt(tab: string): boolean {
  const s = load();
  tick(s);
  save(s);
  if (!s.token) return !s.lockReason;
  if (s.boundTab === tab) return true;
  return !present(s, s.boundTab);
}

/** Unlock / setup / recover / restore / change password: a new session bound to `tab`. */
export function newSession(tab: string | null): string {
  const s = load();
  tick(s);
  if (s.token) s.moved = [s.token, ...s.moved].slice(0, 4);
  s.token = newToken();
  s.boundTab = tab;
  s.lockReason = null;
  s.pretendOther = false;
  if (tab) s.seen[tab] = Date.now();
  save(s);
  return s.token;
}

/** The pretend server restarted (an update): every session ends; the next screen is Sign in. */
export function restartServer() {
  const s = load();
  lockServer(s, 'shutdown');
  s.lockReason = null;
  save(s);
}

export function lock(reason: Reason) {
  const s = load();
  lockServer(s, reason);
  save(s);
}

export function closing(tab: string | null) {
  if (!tab) return;
  const s = load();
  tick(s);
  if (s.boundTab === tab) s.pendingClose = { tab, at: Date.now() + CLOSE_GRACE };
  else delete s.seen[tab];
  save(s);
}

/** POST /api/app/handoff by the window that holds the session. */
export function handoff(fromToken: string | null, toTab: string): { token: string } | 'unauthorized' | 'not_found' {
  const s = load();
  tick(s);
  if (!fromToken || fromToken !== s.token) return 'unauthorized';
  if (toTab === s.boundTab) {
    save(s);
    return 'not_found';
  }
  if (!present(s, toTab)) {
    save(s);
    return 'not_found';
  }
  s.moved = [s.token, ...s.moved].slice(0, 4);
  s.token = newToken();
  s.boundTab = toTab;
  s.seen[toTab] = Date.now();
  save(s);
  return { token: s.token };
}
