/**
 * FinTrack's windows talking to each other (same origin only): BroadcastChannel('fintrack').
 *
 * "Open here" (MAPPING conflict 3): the new window asks with `handoff-request`; the window that
 * has FinTrack open calls POST /api/app/handoff (the server rotates the token) and answers with
 * `handoff` carrying the new token for that window only (matched by tab id and a one-time
 * nonce). No answer within 1.5 s → the new window asks for the password instead.
 *
 * The token is only ever sent to same-origin pages of FinTrack itself, like a duplicated tab's
 * sessionStorage copy. It is never logged.
 *
 * Duplicated tabs: the browser copies sessionStorage into a duplicate, so it would start with the
 * other window's tab id and session token (two windows sharing one session). At start each
 * window says `hello` with its id; a live window with the same id answers `hello-taken`, and the
 * newcomer takes a new id and drops the copied token (`ensureOwnTabId`, before the first status).
 */
import { getTabId, renewTabId } from './tab';

export type TabMessage =
  | { type: 'handoff-request'; from: string; nonce: string }
  | { type: 'handoff'; to: string; nonce: string; token: string }
  | { type: 'handoff-refused'; to: string; nonce: string }
  | { type: 'unlocked'; tab: string }
  | { type: 'focus-request' }
  | { type: 'hello'; tab: string; nonce: string }
  | { type: 'hello-taken'; tab: string; nonce: string };

const NAME = 'fintrack';
export const HANDOFF_TIMEOUT_MS = 1500;
/** How long a new window waits for "that id is mine" before keeping its id. */
export const HELLO_TIMEOUT_MS = 250;

function channel(): BroadcastChannel | null {
  try {
    return typeof BroadcastChannel === 'function' ? new BroadcastChannel(NAME) : null;
  } catch {
    return null;
  }
}

function isMessage(v: unknown): v is TabMessage {
  if (!v || typeof v !== 'object') return false;
  const t = (v as { type?: unknown }).type;
  return (
    t === 'handoff-request' ||
    t === 'handoff' ||
    t === 'handoff-refused' ||
    t === 'unlocked' ||
    t === 'focus-request' ||
    t === 'hello' ||
    t === 'hello-taken'
  );
}

// ---------------------------------------------------------------- duplicated tabs

/** This window's own `hello` (its own listeners hear it too and must not answer it). */
let ownHello: string | null = null;
let responder: BroadcastChannel | null = null;
let idCheck: Promise<boolean> | null = null;

/** For the page's lifetime: answer a `hello` carrying this window's id ("that id is mine"). */
function answerHellos(): void {
  if (responder) return;
  const ch = channel();
  if (!ch) return;
  responder = ch;
  ch.onmessage = (e: MessageEvent) => {
    const m: unknown = e.data;
    if (!isMessage(m) || m.type !== 'hello' || m.nonce === ownHello || m.tab !== getTabId()) return;
    ch.postMessage({ type: 'hello-taken', tab: m.tab, nonce: m.nonce } satisfies TabMessage);
  };
}

/**
 * Make sure no other open FinTrack window uses this window's id (a duplicated tab copies it).
 * Resolves true when it did: this window then has a new id, and the caller must drop the copied
 * session token. Runs once per page; later calls get the same answer.
 */
export function ensureOwnTabId(timeoutMs = HELLO_TIMEOUT_MS): Promise<boolean> {
  if (idCheck) return idCheck;
  idCheck = new Promise<boolean>((resolve) => {
    answerHellos();
    const ch = channel();
    if (!ch) {
      resolve(false);
      return;
    }
    const me = getTabId();
    const n = nonce();
    ownHello = n;
    let done = false;
    const finish = (taken: boolean) => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      ch.close();
      if (taken) renewTabId();
      resolve(taken);
    };
    const timer = window.setTimeout(() => finish(false), timeoutMs);
    ch.onmessage = (e: MessageEvent) => {
      const m: unknown = e.data;
      if (isMessage(m) && m.type === 'hello-taken' && m.tab === me && m.nonce === n) finish(true);
    };
    ch.postMessage({ type: 'hello', tab: me, nonce: n } satisfies TabMessage);
  });
  return idCheck;
}

/** Listen for other windows' messages. Returns a function that stops listening. */
export function listenTabs(fn: (msg: TabMessage) => void): () => void {
  const ch = channel();
  if (!ch) return () => {};
  ch.onmessage = (e: MessageEvent) => {
    if (isMessage(e.data)) fn(e.data);
  };
  return () => ch.close();
}

/** Tell the other windows something (fire and forget). */
export function postTabs(msg: TabMessage): void {
  const ch = channel();
  if (!ch) return;
  try {
    ch.postMessage(msg);
  } finally {
    ch.close();
  }
}

/** A handed-over session token as the server makes them (non-empty, at most 128 characters). */
export function isHandoffToken(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0 && v.length <= 128;
}

function nonce(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

/** The last handoff this window asked for (a late answer after the time limit is still taken). */
let lastNonce: string | null = null;
export function pendingHandoffNonce(): string | null {
  return lastNonce;
}
export function clearHandoffNonce(): void {
  lastNonce = null;
}

/**
 * Ask the window that has FinTrack open to hand its session to this one. Resolves with the new
 * token, or null when no window answers in time or it couldn't (then ask for the password).
 */
export function requestHandoff(timeoutMs = HANDOFF_TIMEOUT_MS): Promise<string | null> {
  const ch = channel();
  if (!ch) return Promise.resolve(null);
  const me = getTabId();
  const n = nonce();
  lastNonce = n;
  return new Promise((resolve) => {
    let done = false;
    const finish = (token: string | null) => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      ch.close();
      resolve(token);
    };
    const timer = window.setTimeout(() => finish(null), timeoutMs);
    ch.onmessage = (e: MessageEvent) => {
      const m: unknown = e.data;
      if (!isMessage(m) || !('to' in m) || m.to !== me || m.nonce !== n) return;
      // Superseded (a newer request) or already taken (the late-answer listener adopted it).
      if (lastNonce !== n) {
        finish(null);
        return;
      }
      if (m.type === 'handoff' && isHandoffToken(m.token)) {
        lastNonce = null;
        finish(m.token);
      } else if (m.type === 'handoff-refused') {
        lastNonce = null;
        finish(null);
      }
    };
    ch.postMessage({ type: 'handoff-request', from: me, nonce: n } satisfies TabMessage);
  });
}
