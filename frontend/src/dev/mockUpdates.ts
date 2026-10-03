/**
 * DEV-ONLY: the updates API (design D5, MAPPING API table) for the mock, with a pretend
 * install that backs up, installs, restarts (the pretend server stops answering for a few
 * seconds and comes back with a new boot id) and can fail at each stage.
 *
 *   &update=banner      an update (1.4) was found in Downloads: the banner shows
 *   &update=news        …and What's new opens by itself
 *   &update=progress    an install is already running (as after a reload mid-install)
 *   &update=success     the update went in: "FinTrack is up to date (version 1.4)" after sign-in
 *   &update=failed      the last try didn't install (FT-UPD-03): state 5 after sign-in
 *   &update=help        What's new opens; Install says it needs Sam's help (FT-UPD-HELP-SPACE)
 *   &update=settings    an update is ready but the banner was put off until tomorrow (Settings)
 *   &update=helpoffer   update 2.0 needs Sam's help: no banner, Settings says so
 *   &update=rejected | damaged | older | same | failedfile
 *                       any file picked in Settings gets that answer
 *   &update=git | notinstalled   this copy doesn't update itself
 *
 * Iron Owl copies that update from GitHub (source "github"; Settings › Colors, text size and
 * updates shows "Check now", the last check and the "once a day" switch):
 *   &update=github          up to date, checked 3 hours ago; Check now finds nothing new
 *   &update=github-found    up to date; Check now finds version 1.4 (banner: "A new version of Iron Owl…")
 *   &update=github-new      version 1.4 was found on GitHub: the Iron Owl banner shows
 *   &update=github-error    the last check couldn't reach GitHub (amber); Check now works
 *   &update=github-nokeys   no GitHub key in this copy: "Updates can't be checked yet" (Check now too)
 *   &update=github-off      the "Check for updates once a day" switch is off
 *   &update=sourceoff       UPDATE_SOURCE=off in the settings file: "Updates are turned off…"
 *
 *   &install=ok (default) | fail (FT-UPD-02 while installing) | help (409 needs_help) |
 *            migfail (the new version starts, then sign-in rolls back: FT-UPD-05) |
 *            crash (the new version never answers; the previous one comes back: FT-UPD-03)
 *
 * Without &update=… the picked file's name decides: "bad"/"fake" → signature, "notupd" or not
 * .ftupdate → not an update, "damaged" → damaged, "big" → too big (413), "old" → older,
 * "same" → same, "failed" → failed before, "help" → needs help, "FinTrack-1.5.0.ftupdate" →
 * ready as 1.5. With no &update at all FinTrack is up to date (1.3) and nothing is offered.
 */
import type { UpdateOffer, UpdateProgress, UpdateResult, UpdateSource, UpdateStatus, UpdateStep } from '../api';
import * as presence from './mockPresence';

type Scenario =
  | 'banner'
  | 'news'
  | 'progress'
  | 'success'
  | 'failed'
  | 'help'
  | 'settings'
  | 'rejected'
  | 'older'
  | 'same'
  | 'damaged'
  | 'failedfile'
  | 'helpoffer'
  | 'git'
  | 'notinstalled'
  | 'github'
  | 'github-found'
  | 'github-new'
  | 'github-error'
  | 'github-nokeys'
  | 'github-off'
  | 'sourceoff';
type InstallMode = 'ok' | 'fail' | 'help' | 'migfail' | 'crash';

const qs = new URLSearchParams(window.location.search);
const scenario = qs.get('update') as Scenario | null;
const installMode: InstallMode = (qs.get('install') as InstallMode | null) ?? (scenario === 'help' ? 'help' : 'ok');
const contact = qs.get('support') === 'none' ? null : 'Sam';

const json = (status: number, body: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const nowIso = () => new Date().toISOString();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const disp = (v: string) => {
  const [a, b, c] = v.split('.');
  return c && c !== '0' ? `${a}.${b}.${c}` : `${a}.${b}`;
};
const cmp = (a: string, b: string) => {
  const x = a.split('.').map(Number);
  const y = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) - (y[i] ?? 0);
  return 0;
};
const localDate = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const tomorrow = () => {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  return localDate(d);
};

const NOTES = [
  'A new home screen shows what needs you first.',
  'Budget uses simpler words: Planned, Spent and Left.',
  'You can print a recovery sheet in case you forget your password.',
  'A Larger text option in Settings.',
];

function makeOffer(version: string, patch: Partial<UpdateOffer> = {}): UpdateOffer {
  return {
    id: `upd-${version.replace(/\./g, '-')}-${Math.random().toString(16).slice(2, 8)}`,
    version,
    display_version: disp(version),
    released_at: nowIso(),
    notes: NOTES,
    kind: 'app',
    minutes: 1,
    size_bytes: 912_384,
    source: 'downloads',
    file_name: `FinTrack-${version}.ftupdate`,
    can_install: true,
    help: null,
    ...patch,
  };
}

// ---------------------------------------------------------------- pretend server state

const mode: UpdateStatus['mode'] = scenario === 'git' ? 'git' : scenario === 'notinstalled' ? 'not_installed' : 'enabled';
// Without &update the mock is simply up to date on 1.4 (Home's status line), as before.
let current = scenario && scenario !== 'success' ? '1.3.0' : '1.4.0';
const installedAt = new Date(Date.now() - 40 * 86_400_000).toISOString();
let lastUpdateAt: string | null = scenario === 'success' ? nowIso() : new Date(Date.now() - 12 * 86_400_000).toISOString();
let bootNo = 1;
let offer: UpdateOffer | null = null;
let remindAfter: string | null = null;
let lastResult: UpdateResult | null = null;
let failedProgress: UpdateProgress | null = null;
let downFrom = 0;
let downUntil = 0;
let rollbackPending = false;
let rollbackAt = 0;

// The update source (GitHub scenarios), as the server words its check problems.
const CHECK_TEXT = {
  offline: 'Iron Owl couldn’t reach GitHub to check for updates. It will try again later.',
  no_keys: 'Updates can’t be checked yet. This copy of Iron Owl doesn’t have the key it needs to make sure an update is real.',
};
const isGithub = !!scenario && scenario.startsWith('github');
const source: UpdateSource = isGithub ? 'github' : scenario === 'sourceoff' ? 'off' : 'file';
let autoCheck = scenario !== 'github-off';
let lastCheck: string | null = isGithub ? new Date(Date.now() - 3 * 3_600_000).toISOString() : null;
let checkError: string | null = scenario === 'github-error' ? CHECK_TEXT.offline : scenario === 'github-nokeys' ? CHECK_TEXT.no_keys : null;
if (scenario === 'github-nokeys') lastCheck = null;
let lastTry = 0;

interface Job {
  offer: UpdateOffer;
  start: number;
  mode: InstallMode;
}
let job: Job | null = null;

if (mode === 'enabled') {
  if (scenario === 'success') {
    lastResult = { outcome: 'installed', from_version: '1.3.0', to_version: '1.4.0', code: null, at: nowIso(), backup_kept: true };
  } else if (scenario === 'failed') {
    lastResult = { outcome: 'failed', from_version: '1.3.0', to_version: '1.4.0', code: 'FT-UPD-03', at: new Date(Date.now() - 3 * 60_000).toISOString(), backup_kept: true };
  } else if (scenario === 'helpoffer') {
    offer = makeOffer('2.0.0', { kind: 'full', minutes: 3, can_install: false, help: { reason: 'reinstall', code: 'FT-UPD-HELP-REINSTALL' } });
  } else if (scenario === 'github-new') {
    offer = makeOffer('1.4.0', { source: 'github', file_name: 'Iron-Owl-1.4.0.ftupdate' });
  } else if (scenario && !isGithub && scenario !== 'sourceoff') {
    offer = makeOffer('1.4.0');
    if (scenario === 'settings') remindAfter = tomorrow();
    if (scenario === 'progress') job = { offer, start: Date.now(), mode: installMode };
  }
}

function bootId() {
  return `mock-boot-${bootNo}`;
}

/** The step a running job is on (lazy clock: called on every request). */
function tick() {
  const now = Date.now();
  if (job) {
    const t = now - job.start;
    if (job.mode === 'fail' && t >= 4500) {
      const at = nowIso();
      failedProgress = { id: job.offer.id, to_version: job.offer.version, step: 'install', state: 'failed', code: 'FT-UPD-02', at };
      lastResult = { outcome: 'failed', from_version: current, to_version: job.offer.version, code: 'FT-UPD-02', at, backup_kept: true };
      job = null;
    } else if (t >= 6000 && downUntil < job.start + 10_000) {
      downFrom = job.start + 6000;
      downUntil = job.start + 10_000;
      // The restart ends every session.
      presence.restartServer();
    }
    if (job && t >= 10_000) finishRestart(job);
  }
  if (rollbackAt && now >= rollbackAt) {
    rollbackAt = 0;
    bootNo += 1;
    const to = current;
    current = '1.3.0';
    rollbackPending = false;
    lastResult = { outcome: 'rolled_back', from_version: '1.3.0', to_version: to, code: 'FT-UPD-05', at: nowIso(), backup_kept: true };
    presence.restartServer();
  }
}

function finishRestart(j: Job) {
  job = null;
  bootNo += 1;
  const from = current;
  if (j.mode === 'crash') {
    // The new version never answered: the launcher went back to the previous one.
    lastResult = { outcome: 'failed', from_version: from, to_version: j.offer.version, code: 'FT-UPD-03', at: nowIso(), backup_kept: true };
  } else {
    current = j.offer.version;
    lastUpdateAt = nowIso();
    if (j.mode === 'migfail') rollbackPending = true;
    else lastResult = { outcome: 'installed', from_version: from, to_version: current, code: null, at: nowIso(), backup_kept: true };
  }
  offer = null;
  remindAfter = null;
  presence.restartServer();
}

function progressNow(): UpdateProgress | null {
  if (!job) return null;
  const t = Date.now() - job.start;
  const step: UpdateStep = t < 2500 ? 'backup' : t < 5000 ? 'install' : 'restart';
  return { id: job.offer.id, to_version: job.offer.version, step, state: step === 'restart' ? 'restarting' : 'running', code: null, at: new Date(job.start).toISOString() };
}

function status(): UpdateStatus {
  const enabled = mode === 'enabled';
  const o = enabled && source !== 'off' ? offer : null;
  const put = !!remindAfter && localDate(new Date()) < remindAfter;
  return {
    mode,
    support_contact: contact,
    current: { version: current, display_version: disp(current), installed_at: installedAt, last_update_at: enabled ? lastUpdateAt : null },
    offer: o,
    banner: { show: !!o && o.can_install && !put && !job, remind_after: put ? remindAfter : null },
    install: progressNow(),
    last_result: lastResult,
    rollback: { available: false, to_version: null },
    source: enabled ? source : 'off',
    auto_check: enabled && source === 'github' && autoCheck,
    last_check: enabled && source === 'github' ? lastCheck : null,
    check_error: enabled && source === 'github' ? checkError : null,
  };
}

/** POST /api/update/scan {now: true} in a GitHub copy: a pretend check (about 1.5 s). */
async function checkGithub(): Promise<Response> {
  if (Date.now() - lastTry < 60_000) return json(200, status()); // at most once a minute
  lastTry = Date.now();
  await sleep(1500);
  if (scenario === 'github-nokeys') return json(200, status());
  checkError = null;
  lastCheck = nowIso();
  if (scenario === 'github-found' && !offer && cmp(current, '1.4.0') < 0) {
    offer = makeOffer('1.4.0', { source: 'github', file_name: 'Iron-Owl-1.4.0.ftupdate' });
    remindAfter = null;
  }
  return json(200, status());
}

// ---------------------------------------------------------------- hooks for mock.ts

/** While the pretend server restarts, nothing answers (mock.ts throws like a network error). */
export function serverDown(): boolean {
  tick();
  const now = Date.now();
  return now >= downFrom && now < downUntil;
}

/** The version the pretend server runs (Home's status line). */
export function mockVersion(): string {
  tick();
  return current;
}

export function health() {
  tick();
  return json(200, { app: 'fintrack', version: current, boot_id: bootId(), window_open: true, web: true });
}

/** A correct password while an update is pending and its data step "fails" (&install=migfail). */
export function onUnlock(): Response | null {
  tick();
  if (!rollbackPending) return null;
  rollbackPending = false;
  const to = '1.3.0';
  // The server answers, stops ~1 s later, and the launcher starts the previous version.
  downFrom = Date.now() + 1000;
  downUntil = Date.now() + 5000;
  rollbackAt = downUntil;
  return json(409, { detail: 'update_rolled_back', code: 'FT-UPD-05', to_version: to });
}

/** POST /api/update/file: what the picked file is. */
async function checkFile(form: FormData | null): Promise<Response> {
  const f = form?.get('file');
  const name = f instanceof File ? f.name : 'update.ftupdate';
  await sleep(1100);
  const lower = name.toLowerCase();
  const forced = scenario;
  const leaf = name.split(/[\\/]/).pop() ?? name;
  const rejected = (code: 'FT-UPD-SIG' | 'FT-UPD-NOTUPD' | 'FT-UPD-DMG', reason: 'signature' | 'not_update' | 'damaged') =>
    json(200, { result: 'rejected', reason, code, file_name: leaf });
  if (forced === 'rejected' || /bad|fake/.test(lower)) return rejected('FT-UPD-SIG', 'signature');
  if (forced === 'damaged' || /damaged|broken/.test(lower)) return rejected('FT-UPD-DMG', 'damaged');
  if (/big/.test(lower)) return json(413, { detail: 'too_large', code: 'FT-UPD-BIG' });
  if (/notupd/.test(lower) || !lower.endsWith('.ftupdate')) return rejected('FT-UPD-NOTUPD', 'not_update');
  const m = /(\d+\.\d+\.\d+)/.exec(name);
  if (forced === 'older' || /old/.test(lower))
    return json(200, { result: 'older', code: 'FT-UPD-OLD', file_name: leaf, file_version: '1.2.0', current_version: current });
  if (forced === 'same' || /same/.test(lower))
    return json(200, { result: 'same', code: 'FT-UPD-SAME', file_name: leaf, file_version: current, current_version: current });
  if (forced === 'failedfile' || /failed/.test(lower))
    return json(200, { result: 'failed', code: 'FT-UPD-03', file_name: leaf, file_version: m?.[1] ?? '1.4.0', current_version: current });
  const version = m?.[1] ?? (cmp(current, '1.4.0') < 0 ? '1.4.0' : '1.5.0');
  if (cmp(version, current) < 0)
    return json(200, { result: 'older', code: 'FT-UPD-OLD', file_name: leaf, file_version: version, current_version: current });
  if (cmp(version, current) === 0)
    return json(200, { result: 'same', code: 'FT-UPD-SAME', file_name: leaf, file_version: version, current_version: current });
  const needsHelp = forced === 'helpoffer' || /help/.test(lower);
  offer = makeOffer(version, {
    source: 'picked',
    file_name: leaf,
    ...(needsHelp ? { kind: 'full' as const, minutes: 3, can_install: false, help: { reason: 'reinstall' as const, code: 'FT-UPD-HELP-REINSTALL' } } : {}),
  });
  remindAfter = null;
  return json(200, { result: 'ready', offer });
}

/** /api/update/* (after mock.ts checked the session). */
export async function handleUpdates(method: string, path: string, body: Record<string, unknown>, form: FormData | null): Promise<Response | null> {
  if (!path.startsWith('/api/update/')) return null;
  tick();
  if (path === '/api/update/status' && method === 'GET') return json(200, status());
  if (path === '/api/update/progress' && method === 'GET') {
    const p = progressNow() ?? failedProgress;
    return p ? json(200, p) : new Response(null, { status: 204 });
  }
  const off = mode !== 'enabled' || source === 'off';
  if (off && (method === 'POST' || method === 'PUT') && path !== '/api/update/result/ack') return json(409, { detail: 'disabled', code: 'disabled' });
  if (path === '/api/update/scan' && method === 'POST') {
    if (source === 'github' && body.now === true) return checkGithub();
    return json(200, status());
  }
  if (path === '/api/update/auto-check' && method === 'PUT') {
    if (source !== 'github') return json(409, { detail: 'disabled', code: 'disabled' });
    if (typeof body.on !== 'boolean') return json(422, { detail: 'on must be true or false' });
    autoCheck = body.on;
    return json(200, status());
  }
  if (path === '/api/update/file' && method === 'POST') {
    if (job) return json(429, { detail: 'busy', code: 'busy' });
    return checkFile(form);
  }
  if (path === '/api/update/dismiss' && method === 'POST') {
    if (!offer || body.id !== offer.id) return json(404, { detail: 'stale_offer', code: 'stale_offer' });
    remindAfter = tomorrow();
    return json(200, status());
  }
  if (path === '/api/update/install' && method === 'POST') {
    if (job) return json(409, { detail: 'busy', code: 'busy' });
    if (!offer || body.id !== offer.id) return json(404, { detail: 'stale_offer', code: 'stale_offer' });
    if (!offer.can_install) return json(409, { detail: 'needs_help', code: offer.help?.code, help: offer.help });
    if (installMode === 'help')
      return json(409, { detail: 'needs_help', code: 'FT-UPD-HELP-SPACE', help: { reason: 'disk_space', code: 'FT-UPD-HELP-SPACE' } });
    failedProgress = null;
    job = { offer, start: Date.now(), mode: installMode };
    return json(202, progressNow());
  }
  if (path === '/api/update/result/ack' && method === 'POST') {
    if (lastResult && body.at === lastResult.at) {
      lastResult = null;
      failedProgress = null;
    }
    return new Response(null, { status: 204 });
  }
  if (path === '/api/update/rollback' && method === 'POST') return json(409, { detail: 'rollback_unavailable', code: 'rollback_unavailable' });
  return json(404, { detail: `mock: no route for ${method} ${path}` });
}
