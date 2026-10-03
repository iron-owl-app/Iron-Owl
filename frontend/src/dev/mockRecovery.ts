/**
 * DEV-ONLY: the recovery sheet endpoints for the mock API (MAPPING §3), plus the shared
 * "too many tries" limiter that unlock, change-password and the sheet all count against.
 *
 *   &rec=active (default) | none | unconfirmed | stale   the vault's sheet (mock=norecovery → none)
 *   &support=Sam (default) | none                     /api/auth/status support_contact
 *   &lockout=130                                         start in a "too many tries" wait (seconds)
 *   &tries=2                                             tries left before the wait starts
 *   &pending=expire                                      Done on a new sheet → 409 pending_expired
 *   &pending=finish                                      Done on a new sheet → 500 finish_on_unlock
 *   &unusable=1                                          the right sheet → 409 unusable
 *
 * Sheet 482-913 (active): 739151 204868 581233 916405 362581 047913 (also what setup shows).
 * Sheet 305-118 (retired → "out of date"): 618274 403951 172641 850313 294762 531081.
 * Any other 36 digits with correct check digits → "don't match" (counted). Mock password:
 * `correct horse battery`. The numbers are never logged.
 */
import { GROUPS, badGroups, cleanDigits, groupCheck } from '../lib/recoveryCode';
import type { RecoveryState } from '../api';
import { mockSupportContact } from './mockSettings';

const qs = new URLSearchParams(window.location.search);

const withChecks = (bases: string[]) => bases.map((b, i) => b + String(groupCheck(i + 1, b)));
const ACTIVE = withChecks(['73915', '20486', '58123', '91640', '36258', '04791']);
const RETIRED = withChecks(['61827', '40395', '17264', '85031', '29476', '53108']);

interface MockSheet {
  code: string;
  sheet: string;
  created_at: string;
  confirmed: boolean;
  stale: boolean;
}

const daysAgoIso = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();

function initialSheet(scenario: string): MockSheet | null {
  const mode = qs.get('rec') ?? (scenario === 'norecovery' || scenario === 'setup' ? 'none' : 'active');
  if (mode === 'none') return null;
  return { code: ACTIVE.join(''), sheet: '482-913', created_at: daysAgoIso(40), confirmed: mode !== 'unconfirmed', stale: mode === 'stale' };
}

const rec = {
  sheet: null as MockSheet | null,
  retired: [{ code: RETIRED.join(''), sheet: '305-118' }] as { code: string; sheet: string }[],
  pending: null as { id: string; code: string; sheet: string; created_at: string } | null,
};

export function initRecovery(scenario: string) {
  rec.sheet = initialSheet(scenario);
  const lock = Number(qs.get('lockout') ?? 0);
  if (qs.get('reset') === '1') limiter.reset();
  if (lock > 0) limiter.set(5, Date.now() + lock * 1000);
  else if (qs.get('tries')) limiter.set(Math.max(0, 5 - Number(qs.get('tries'))), 0);
}

// ---------------------------------------------------------------- limiter (shared)

const THRESHOLD = 5;
/**
 * Kept in localStorage like the rest of the pretend server (mockPresence), so a reload keeps
 * counting and keeps waiting, and every mock tab shares it (as the real server's limiter does).
 */
const LIMITER_KEY = 'fintrack.mock.limiter';
export const limiter = {
  failures: 0,
  until: 0,
  load() {
    try {
      const raw = window.localStorage.getItem(LIMITER_KEY);
      const v = raw ? (JSON.parse(raw) as { failures?: unknown; until?: unknown }) : null;
      this.failures = v && typeof v.failures === 'number' ? v.failures : 0;
      this.until = v && typeof v.until === 'number' ? v.until : 0;
    } catch {
      /* ignore */
    }
  },
  set(failures: number, until: number) {
    this.failures = failures;
    this.until = until;
    try {
      window.localStorage.setItem(LIMITER_KEY, JSON.stringify({ failures, until }));
    } catch {
      /* ignore */
    }
  },
  /** Seconds left in the wait, or 0. */
  wait(): number {
    this.load();
    return Math.max(0, Math.ceil((this.until - Date.now()) / 1000));
  },
  /** Wrong tries left before a wait (0 during and after one, like the server: no reset after a wait). */
  left(): number {
    this.load();
    return Math.max(0, THRESHOLD - this.failures);
  },
  fail(): number {
    this.load();
    const failures = this.failures + 1;
    this.set(failures, failures >= THRESHOLD ? Date.now() + Math.min(900, 30 * 2 ** (failures - THRESHOLD)) * 1000 : this.until);
    return Math.max(0, THRESHOLD - failures);
  },
  reset() {
    this.set(0, 0);
  },
};

type Json = (status: number, body: unknown) => Response;

/** 429 when waiting (the real server sends Retry-After too). */
export function guard(json: Json): Response | null {
  const s = limiter.wait();
  if (!s) return null;
  const r = json(429, { detail: 'too many attempts', code: 'rate_limited', retry_after: s });
  r.headers.set('Retry-After', String(s));
  return r;
}

/** A counted wrong try: 401 with tries_left, or (like the server) 429 when this one starts a wait. */
function counted(json: Json, body: Record<string, unknown>): Response {
  const left = limiter.fail();
  return guard(json) ?? json(401, { ...body, tries_left: left });
}

export function wrongPassword(json: Json): Response {
  return counted(json, { detail: 'invalid password', code: 'wrong_password' });
}

// ---------------------------------------------------------------- status and setup

export function recoveryStatusFields() {
  const support = qs.get('support') ?? 'Sam';
  const wait = limiter.wait();
  return {
    recovery: { available: !!rec.sheet, sheet: rec.sheet?.sheet ?? null },
    retry_after: wait || null,
    tries_left: limiter.left(),
    support_contact: mockSupportContact(support === 'none' ? null : support),
  };
}

/** The sheet made with a new vault (active but not confirmed until setup's Continue). */
export function setupRecovery() {
  const created_at = new Date().toISOString();
  rec.sheet = { code: ACTIVE.join(''), sheet: '482-913', created_at, confirmed: false, stale: false };
  return { sheet: '482-913', created_at, groups: [...ACTIVE] };
}

function statusOf(): { status: RecoveryState; sheet: string | null; created_at: string | null } {
  const s = rec.sheet;
  if (!s) return { status: 'none', sheet: null, created_at: null };
  return { status: s.stale ? 'stale' : s.confirmed ? 'active' : 'unconfirmed', sheet: s.sheet, created_at: s.created_at };
}

export function clearPending() {
  rec.pending = null;
}

// ---------------------------------------------------------------- forgot password (no session)

/** Checks in the server's order: wait → initialized → format/typo → (weak password) → sheet present → match. */
function checkCode(json: Json, body: Record<string, unknown>, initialized: boolean, newPassword?: unknown): Response | { ok: true } {
  const g = guard(json);
  if (g) return g;
  if (!initialized) return json(409, { detail: 'not initialized', code: 'not_initialized' });
  const raw = body.code;
  if (typeof raw !== 'string' || raw.length > 128 || !/^[0-9 -]*$/.test(raw)) return json(422, { detail: 'bad format', code: 'bad_format' });
  const digits = cleanDigits(raw);
  if (digits.length !== GROUPS * 6) return json(422, { detail: 'bad format', code: 'bad_format' });
  const groups = Array.from({ length: GROUPS }, (_, i) => digits.slice(i * 6, i * 6 + 6));
  const typos = badGroups(groups);
  if (typos.length) return json(422, { detail: 'typo', code: 'typo', groups: typos });
  if (newPassword !== undefined && (typeof newPassword !== 'string' || [...newPassword].length < 12)) {
    return json(422, { detail: 'password must be at least 12 characters', code: 'weak_password' });
  }
  if (!rec.sheet) return json(409, { detail: 'no recovery sheet', code: 'no_recovery' });
  const active_sheet = rec.sheet.sheet;
  if (digits === rec.sheet.code) {
    if (rec.sheet.stale || qs.get('unusable') === '1') return json(409, { detail: 'sheet unusable', code: 'unusable', active_sheet });
    return { ok: true };
  }
  if (rec.retired.some((r) => r.code === digits)) return json(409, { detail: 'sheet outdated', code: 'outdated', active_sheet });
  return counted(json, { detail: 'no match', code: 'no_match', active_sheet });
}

/** POST /api/auth/recover/check and /api/auth/recover (before the "locked" gate). */
export function handleRecoverPublic(
  json: Json,
  method: string,
  path: string,
  body: Record<string, unknown>,
  ctx: { initialized: boolean; onRecovered: (newPassword: string) => void },
): Response | null {
  if (method !== 'POST') return null;
  if (path === '/api/auth/recover/check') {
    const r = checkCode(json, body, ctx.initialized);
    return r instanceof Response ? r : json(200, { ok: true, sheet: rec.sheet!.sheet });
  }
  if (path === '/api/auth/recover') {
    const r = checkCode(json, body, ctx.initialized, body.new_password ?? '');
    if (r instanceof Response) return r;
    limiter.reset();
    rec.pending = null;
    ctx.onRecovered(String(body.new_password));
    return json(200, { ok: true, session_token: 'mock-token' });
  }
  return null;
}

// ---------------------------------------------------------------- Settings (session)

function randomSheetNumber(): string {
  const n = () => String(Math.floor(Math.random() * 1000)).padStart(3, '0');
  let s = `${n()}-${n()}`;
  while (s === rec.sheet?.sheet || rec.retired.some((r) => r.sheet === s)) s = `${n()}-${n()}`;
  return s;
}

function randomGroups(): string[] {
  const bases = Array.from({ length: GROUPS }, () => String(Math.floor(Math.random() * 100000)).padStart(5, '0'));
  return withChecks(bases);
}

/** GET/POST /api/recovery, /activate, /confirm. */
export function handleRecovery(json: Json, method: string, path: string, body: Record<string, unknown>, password: string): Response | null {
  if (path === '/api/recovery' && method === 'GET') return json(200, statusOf());
  if (path === '/api/recovery' && method === 'POST') {
    const g = guard(json);
    if (g) return g;
    const pw = body.current_password;
    if (typeof pw !== 'string' || !pw) return json(422, { detail: 'Enter your current Iron Owl password.', code: 'password_required' });
    if (pw !== password) return wrongPassword(json);
    const groups = randomGroups();
    rec.pending = { id: `p${Date.now().toString(36)}`, code: groups.join(''), sheet: randomSheetNumber(), created_at: new Date().toISOString() };
    return json(200, { pending_id: rec.pending.id, sheet: rec.pending.sheet, created_at: rec.pending.created_at, groups });
  }
  if (path === '/api/recovery/activate' && method === 'POST') {
    const p = rec.pending;
    if (!p || body.pending_id !== p.id || qs.get('pending') === 'expire') {
      rec.pending = null;
      return json(409, { detail: 'pending sheet expired', code: 'pending_expired' });
    }
    const finish = qs.get('pending') === 'finish'; // the new sheet takes, but Done answers 500 (no lock in the mock)
    if (rec.sheet) rec.retired = [{ code: rec.sheet.code, sheet: rec.sheet.sheet }, ...rec.retired].slice(0, 10);
    rec.sheet = { code: p.code, sheet: p.sheet, created_at: p.created_at, confirmed: true, stale: false };
    rec.pending = null;
    if (finish) return json(500, { detail: "Iron Owl couldn't finish this step. Unlock Iron Owl to complete it.", code: 'finish_on_unlock' });
    return json(200, statusOf());
  }
  if (path === '/api/recovery/confirm' && method === 'POST') {
    if (!rec.sheet || body.sheet !== rec.sheet.sheet) return json(409, { detail: 'sheet changed', code: 'sheet_changed' });
    rec.sheet.confirmed = true;
    return json(200, statusOf());
  }
  return null;
}
