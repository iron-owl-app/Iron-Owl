/**
 * Words for the updates screens (design D5, MAPPING decisions 8, 11, 12, 14, 15). Pure functions
 * with no runtime imports, so scripts/updates.test.ts can run them under plain Node.
 *
 * Audience: someone who has never heard of a server, a port or a checksum. Never show those
 * words, or a folder path; a short code goes under Details for whoever the user calls.
 */
import type { UpdateCheck, UpdateHelpReason, UpdateSource, UpdateStep } from '../api';

/** "Sam", or the fallback that fits mid-sentence ("Ask the person who set up FinTrack."). */
export function contactName(contact: string | null | undefined): string {
  const c = contact?.trim();
  return c ? c : 'the person who set up Iron Owl';
}

/** "Sam’s help", or "help from the person who set up FinTrack". */
export function contactHelp(contact: string | null | undefined): string {
  const c = contact?.trim();
  return c ? `${c}’s help` : 'help from the person who set up Iron Owl';
}

/** "1.4" for 1.4.0, "1.4.2" when the patch isn't 0; anything else as it is. */
export function displayVersion(v: string | null | undefined): string {
  if (!v) return '';
  const m = /^(\d+)\.(\d+)(?:\.(\d+))?/.exec(v);
  if (!m) return v;
  return m[3] && m[3] !== '0' ? `${m[1]}.${m[2]}.${m[3]}` : `${m[1]}.${m[2]}`;
}

/** offer.minutes → "about a minute" (1), "a few minutes" (2-5), "about 8 minutes". */
export function minutesPhrase(minutes: number | null | undefined): string {
  const n = typeof minutes === 'number' && Number.isFinite(minutes) ? Math.max(1, Math.round(minutes)) : 1;
  if (n <= 1) return 'about a minute';
  if (n <= 5) return 'a few minutes';
  return `about ${n} minutes`;
}

// ---------------------------------------------------------------- banner, What's new

export const BANNER = {
  region: 'Update available',
  lead: 'An Iron Owl update is ready to install.',
  /** An update found on GitHub (Iron Owl installs from GitHub). */
  leadGithub: 'A new version of Iron Owl is ready to install.',
  detail: (version: string, minutes: number) => `Version ${displayVersion(version)} · takes ${minutesPhrase(minutes)}`,
  install: 'Install update',
  news: 'What’s new',
  notNow: 'Not now',
  /** Read out once when the banner first shows for an update. */
  announce: (version: string) => `An Iron Owl update is ready to install: version ${displayVersion(version)}.`,
  announceGithub: (version: string) => `A new version of Iron Owl is ready to install: version ${displayVersion(version)}.`,
  remindTitle: 'Okay. We’ll remind you tomorrow.',
  remindBody: 'You can also install it from Settings.',
} as const;

export const NEWS = {
  title: (version: string) => `What’s new in Iron Owl ${displayVersion(version)}`,
  info: (minutes: number) =>
    `Installing takes ${minutesPhrase(minutes)}. Iron Owl will make a backup first, restart, and ask for your password again.`,
  later: 'Later',
  install: 'Install update',
  starting: 'Starting…',
} as const;

// ---------------------------------------------------------------- installing

export const STEP_ORDER: UpdateStep[] = ['backup', 'install', 'restart'];
const STEP_LABEL: Record<UpdateStep, string> = { backup: 'Making a backup…', install: 'Installing…', restart: 'Restarting Iron Owl…' };

/** A step's words: "Making a backup…" while current or coming up, "Making a backup" once done. */
export function stepLabel(step: UpdateStep, done: boolean): string {
  const l = STEP_LABEL[step];
  return done ? l.replace(/…$/, '') : l;
}

/** The bar: halfway through the current step (backup 17, install 50, restart 83); 100 when done. */
export function progressPercent(step: UpdateStep, finished = false): number {
  if (finished) return 100;
  const i = STEP_ORDER.indexOf(step);
  return Math.round(((Math.max(0, i) + 0.5) / STEP_ORDER.length) * 100);
}

export const INSTALLING = {
  title: (version: string) => `Updating Iron Owl to ${displayVersion(version)}`,
  body: (minutes: number) => `Please don’t turn off your computer. This takes ${minutesPhrase(minutes)}.`,
  bar: 'Update progress',
  /** For screen readers: "Step 2 of 3: Installing…" */
  stepNews: (step: UpdateStep) => `Step ${STEP_ORDER.indexOf(step) + 1} of ${STEP_ORDER.length}: ${STEP_LABEL[step]}`,
} as const;

export const ROLLING_BACK = {
  title: 'Undoing the update',
  body: 'Iron Owl is going back to the previous version. This takes about a minute.',
} as const;

// ---------------------------------------------------------------- didn't install, needs help

/** "Sep 26, 2026, 9:41 AM" */
export function whenText(at: Date | string | null | undefined, locale?: string): string {
  const d = at instanceof Date ? at : at ? new Date(at) : new Date();
  const ok = !Number.isNaN(d.getTime()) ? d : new Date();
  return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' }).format(ok);
}

export const FAILED = {
  title: 'The update didn’t install.',
  body: (contact: string | null | undefined) =>
    `Iron Owl is still on the previous version and your data wasn’t changed. You can try again later. If it keeps happening, let ${contactName(contact)} know.`,
  back: 'Back to Iron Owl',
} as const;

/** Details under "didn't install": "Code FT-UPD-03 · Sep 26, 2026, 9:41 AM. The backup was kept." */
export function failedDetails(code: string | null | undefined, at: Date | string | null | undefined, backupKept: boolean, locale?: string): string {
  const c = code || 'FT-UPD-02';
  return `Code ${c} · ${whenText(at, locale)}.${backupKept ? ' The backup was kept.' : ''}`;
}

export const HELP = {
  title: (contact: string | null | undefined) => `This update needs ${contactHelp(contact)} to install.`,
  body: (contact: string | null | undefined) =>
    `Nothing was changed. Ask ${contactName(contact)} to install it with you, or leave it for later. Iron Owl keeps working as usual.`,
  back: 'Back to Iron Owl',
} as const;

const HELP_WHY: Record<UpdateHelpReason, string> = {
  reinstall: 'needs a change Iron Owl can’t make on its own',
  python: 'needs a change Iron Owl can’t make on its own',
  launcher: 'needs a newer Iron Owl starter',
  skipped_version: 'needs an earlier update first',
  runtime_missing: 'needs parts this copy of Iron Owl doesn’t have',
  disk_space: 'needs more free space on this computer',
};

/** "Code FT-UPD-HELP-SPACE · update 1.4 needs more free space on this computer." */
export function helpDetails(code: string | null | undefined, reason: UpdateHelpReason | null | undefined, version: string | null | undefined): string {
  const why = (reason && HELP_WHY[reason]) || HELP_WHY.reinstall;
  const v = displayVersion(version);
  return `Code ${code || 'FT-UPD-HELP'} · ${v ? `update ${v}` : 'this update'} ${why}.`;
}

// ---------------------------------------------------------------- a picked file

export interface CheckCopy {
  /** Red "Don't install this file" (alert) or a neutral note. */
  tone: 'danger' | 'neutral';
  title: string;
  body: string;
  /** Second line under the body (red only). */
  note: string | null;
  details: string;
}

const REJECT_WHY: Record<string, string> = {
  'FT-UPD-SIG': 'the signature check failed',
  'FT-UPD-NOTUPD': 'it isn’t an Iron Owl update',
  'FT-UPD-DMG': 'the file is damaged or incomplete',
  'FT-UPD-BIG': 'the file is too big to be an update',
};

/** What to say about a picked file that won't be installed (everything but `ready`). */
export function checkCopy(check: Exclude<UpdateCheck, { result: 'ready' }>, contact: string | null | undefined): CheckCopy {
  const who = contactName(contact);
  const file = check.file_name || 'The file';
  if (check.result === 'rejected') {
    const body =
      check.code === 'FT-UPD-DMG'
        ? `This file is damaged or incomplete. Don’t install it. Ask ${who} to send it again.`
        : check.code === 'FT-UPD-BIG'
          ? `This file is too big to be an Iron Owl update. Don’t install it. Ask ${who}.`
          : `This file isn’t an Iron Owl update, or it’s been changed. Don’t install it. Ask ${who}.`;
    return {
      tone: 'danger',
      title: 'Don’t install this file',
      body,
      note: 'Nothing was installed and your data wasn’t changed. You can delete the file.',
      details: `${file} · ${REJECT_WHY[check.code] ?? 'it can’t be used'} (${check.code})`,
    };
  }
  const fileV = displayVersion(check.file_version);
  const curV = displayVersion(check.current_version);
  if (check.result === 'older') {
    return {
      tone: 'neutral',
      title: 'You already have a newer version',
      body: `This file is Iron Owl ${fileV}; you have ${curV}. Nothing was installed. You can delete the file.`,
      note: null,
      details: `${file} · version ${fileV} (${check.code})`,
    };
  }
  if (check.result === 'same') {
    return {
      tone: 'neutral',
      title: 'This update is already installed',
      body: `Iron Owl is already on version ${curV}. You can delete the file.`,
      note: null,
      details: `${file} · version ${fileV} (${check.code})`,
    };
  }
  return {
    tone: 'neutral',
    title: 'Iron Owl won’t try this update again',
    body: `This update didn’t install before, so Iron Owl won’t try it again. Ask ${who} for a new file.`,
    note: null,
    details: `${file} · version ${fileV} (${check.code})`,
  };
}

// ---------------------------------------------------------------- Settings › About and updates

export const ABOUT = {
  title: 'About and updates',
  version: 'Version',
  lastUpdate: 'Last update',
  updates: 'Updates',
  ready: (version: string) => `Version ${displayVersion(version)} is ready to install`,
  needsHelp: (version: string, contact: string | null | undefined) => `Version ${displayVersion(version)} needs ${contactHelp(contact)} to install`,
  upToDate: 'Up to date',
  git: 'Updated with git',
  gitText: 'This copy runs from a git folder, so it doesn’t look for .ftupdate files. Update it with git pull, then restart Iron Owl.',
  notInstalled: (contact: string | null | undefined) => `This copy of Iron Owl can’t install updates by itself. Ask ${contactName(contact)}.`,
  explain: (contact: string | null | undefined) => {
    const c = contact?.trim();
    return `${c ? `${c} sends updates` : 'Updates come'} by email as a file ending in .ftupdate. Iron Owl finds it in your Downloads folder, or you can pick it here.`;
  },
  install: 'Install update',
  pick: 'Install an update from a file…',
  checking: (file: string) => `Checking ${file}…`,
  never: 'Not yet',
  loadError: 'Iron Owl couldn’t check for updates just now. Please try again in a moment.',
} as const;

/** Settings › Updates for a copy that updates from GitHub (Iron Owl). */
export const GITHUB = {
  explain: 'Iron Owl looks on GitHub once a day for a new version. If one is out, it downloads it, makes sure it really comes from Iron Owl, and asks you before installing.',
  privacy: 'The update check sends nothing about you or your money.',
  auto: 'Check for updates once a day',
  autoHelp: 'Only while Iron Owl is open and unlocked.',
  autoOn: (on: boolean) => (on ? 'Iron Owl will check for updates once a day.' : 'Iron Owl won’t check for updates by itself. You can still click Check now.'),
  lastChecked: (at: string | null | undefined, now?: Date) => `Last checked: ${lastUpdateText(at, now)}`,
  checkNow: 'Check now',
  checking: 'Checking…',
  checked: 'Checked just now.',
  failed: 'Iron Owl couldn’t check for updates just now. Please try again in a moment.',
  switchFailed: 'That didn’t save. Please try again.',
} as const;

/** Settings › Updates when updates are turned off in the settings file. */
export const SOURCE_OFF = 'Updates are turned off for this copy of Iron Owl.';

/** Which banner words fit the update's source. */
export function bannerLead(source: UpdateSource | null | undefined): string {
  return source === 'github' ? BANNER.leadGithub : BANNER.lead;
}
export function bannerAnnounce(source: UpdateSource | null | undefined, version: string): string {
  return source === 'github' ? BANNER.announceGithub(version) : BANNER.announce(version);
}

/** "Today", "Yesterday", else "Oct 4, 2026". */
export function lastUpdateText(at: string | null | undefined, now: Date = new Date(), locale?: string): string {
  if (!at) return ABOUT.never;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return ABOUT.never;
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86_400_000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric', year: 'numeric' }).format(d);
}

// ---------------------------------------------------------------- messages (toasts)

export const MESSAGES = {
  upToDate: (version: string) => `Iron Owl is up to date (version ${displayVersion(version)}).`,
  busy: 'Iron Owl is already checking a file. Please wait a moment and try again.',
  disabled: 'This copy of Iron Owl doesn’t install updates by itself.',
  stale: 'That update isn’t available any more. Iron Owl checked again for you.',
  checkFailed: 'Iron Owl couldn’t check that file. Please try again.',
  installFailed: 'The update couldn’t start. Please try again in a moment.',
  alreadyRunning: 'An update is already being installed.',
  /** The password screen, 409 schema_too_new. */
  tooNew: (contact: string | null | undefined) => `This copy of Iron Owl is older than your data, so it can’t open it. Ask ${contactName(contact)}.`,
} as const;
