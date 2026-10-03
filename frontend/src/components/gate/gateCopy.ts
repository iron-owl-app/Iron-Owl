/**
 * Words for the app-window screens (design D4, MAPPING copy table). Pure functions with no
 * imports at runtime, so scripts/gate.test.ts can run them under plain Node.
 *
 * Audience: someone who doesn't know what a server or a port is. Never say backend, server,
 * port, URL or error; say what happened and the one thing to do.
 */
import type { ServerLockReason } from '../../api';

/** Why the password screen is showing (Sign in = `start`; `here` = "Open here" without the other window). */
export type LockReason = 'start' | 'idle' | 'closed' | 'manual' | 'ended' | 'here';

/** The server's reason for the vault being locked → the screen's reason. */
export function reasonFromServer(r: ServerLockReason | null | undefined, fallback: LockReason = 'start'): LockReason {
  switch (r) {
    case 'idle':
      return 'idle';
    case 'closed':
      return 'closed';
    case 'manual':
      return 'manual';
    case 'moved':
    case 'shutdown':
      return 'ended';
    default:
      return fallback;
  }
}

/** "Good morning" before noon, "Good afternoon" until 6 pm, then "Good evening". */
export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

export interface PasswordCopy {
  title: string;
  body: string;
  button: string;
  busy: string;
  /** The lock icon (locked) or the FinTrack logo (sign in, open here). */
  icon: 'logo' | 'lock';
}

const PICK_UP = 'Enter your password to pick up where you left off.';

export function passwordCopy(reason: LockReason, autoLockMinutes: number, now: Date = new Date()): PasswordCopy {
  const open = { button: 'Open Iron Owl', busy: 'Opening Iron Owl…' };
  const unlock = { button: 'Unlock', busy: 'Unlocking…', icon: 'lock' as const, title: 'Iron Owl is locked' };
  switch (reason) {
    case 'idle': {
      const n = Math.max(1, Math.round(autoLockMinutes));
      return { ...unlock, body: `You were away for ${n} ${n === 1 ? 'minute' : 'minutes'}, so Iron Owl locked itself to keep your data safe. ${PICK_UP}` };
    }
    case 'closed':
      return { ...unlock, body: 'Iron Owl was closed. Enter your password to open it again.' };
    case 'manual':
      return { ...unlock, body: `You locked Iron Owl. ${PICK_UP}` };
    case 'ended':
      return { ...unlock, body: `Iron Owl locked itself to keep your data safe. ${PICK_UP}` };
    case 'here':
      return { ...open, icon: 'logo', title: 'Open Iron Owl here', body: 'Enter your password to open Iron Owl in this window. The other window will lock.' };
    case 'start':
    default:
      return { ...open, icon: 'logo', title: greeting(now), body: 'Enter your Iron Owl password to open your data.' };
  }
}

/** "That password isn't right. You have 3 tries left before a short wait." */
export function wrongPasswordLine(triesLeft: number | null): string {
  if (triesLeft === null || triesLeft <= 0) return 'That password isn’t right. Try again.';
  return `That password isn’t right. You have ${triesLeft} ${triesLeft === 1 ? 'try' : 'tries'} left before a short wait.`;
}

/** "30 seconds", "1 minute", "2 minutes", "2 minutes 30 seconds" (the length of a wait, in words). */
export function waitWords(seconds: number): string {
  const s = Math.max(1, Math.ceil(seconds));
  if (s < 60) return `${s} ${s === 1 ? 'second' : 'seconds'}`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  const min = `${m} ${m === 1 ? 'minute' : 'minutes'}`;
  return r ? `${min} ${r} ${r === 1 ? 'second' : 'seconds'}` : min;
}

export function lockoutLine(seconds: number): string {
  return `Too many tries. Please wait ${waitWords(seconds)}, then try again.`;
}

/** "Please wait 29s" / "Please wait 1:58" on the button while waiting. */
export function waitButton(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return `Please wait ${s}s`;
  return `Please wait ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export const AFTER_WAIT = 'You can try again now. Another wrong try means a longer wait.';

// ---------------------------------------------------------------- FinTrack isn't running

/** FT-UPD-04: FinTrack restarted for an update and never answered again. */
export type UnreachableCode = 'FT-START-02' | 'FT-RUN-01' | 'FT-UPD-04';

/** "Code FT-START-02 · Sep 26, 2026, 9:14 AM. If you call Sam, read them this code." */
export function detailsLine(code: string, at: Date, contact: string | null, locale?: string): string {
  const when = new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' }).format(at);
  const who = contact ? `If you call ${contact}, read them this code.` : 'If you ask for help, read out this code.';
  return `Code ${code} · ${when}. ${who}`;
}

// ---------------------------------------------------------------- already open

export type ElsewhereKind = 'second' | 'moved';

export const ELSEWHERE = {
  second: {
    title: 'Iron Owl is already open in another window',
    body: 'Use that window, or open Iron Owl here instead. The other window will lock.',
  },
  moved: {
    title: 'Iron Owl is open in another window',
    body: 'You opened Iron Owl in another window, so this one locked. You can close this window.',
  },
  opening: 'Opening Iron Owl here… The other window will lock.',
  useOther:
    'Okay. You can close this window. Iron Owl is still open in the other one. Look for it on the taskbar at the bottom of your screen.',
} as const;
