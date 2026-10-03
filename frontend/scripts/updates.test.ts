/**
 * Unit tests for src/updates/copy.ts (the updates screens' words, design D5 / MAPPING decisions
 * 8, 11, 12, 14, 15). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ABOUT,
  BANNER,
  FAILED,
  GITHUB,
  HELP,
  SOURCE_OFF,
  bannerAnnounce,
  bannerLead,
  checkCopy,
  contactHelp,
  contactName,
  displayVersion,
  failedDetails,
  helpDetails,
  lastUpdateText,
  minutesPhrase,
  progressPercent,
  stepLabel,
} from '../src/updates/copy.ts';
import { afterPollError, RESTART_LIMIT_MS, restartedBack, restartOverdue } from '../src/updates/watch.ts';

test('contact names and the fallback', () => {
  assert.equal(contactName('Sam'), 'Sam');
  assert.equal(contactName('  '), 'the person who set up Iron Owl');
  assert.equal(contactName(null), 'the person who set up Iron Owl');
  assert.equal(contactHelp('Sam'), 'Sam’s help');
  assert.equal(contactHelp(null), 'help from the person who set up Iron Owl');
  assert.equal(HELP.title('Sam'), 'This update needs Sam’s help to install.');
});

test('versions show major.minor, and the patch only when it isn’t 0', () => {
  assert.equal(displayVersion('1.4.0'), '1.4');
  assert.equal(displayVersion('1.4.2'), '1.4.2');
  assert.equal(displayVersion('2.0'), '2.0');
  assert.equal(displayVersion(''), '');
});

test('minutes read as plain words', () => {
  assert.equal(minutesPhrase(1), 'about a minute');
  assert.equal(minutesPhrase(3), 'a few minutes');
  assert.equal(minutesPhrase(9), 'about 9 minutes');
  assert.equal(BANNER.detail('1.4.0', 1), 'Version 1.4 · takes about a minute');
});

test('steps drop the … once done; the bar sits halfway through the current step', () => {
  assert.equal(stepLabel('backup', false), 'Making a backup…');
  assert.equal(stepLabel('backup', true), 'Making a backup');
  assert.equal(progressPercent('backup'), 17);
  assert.equal(progressPercent('install'), 50);
  assert.equal(progressPercent('restart'), 83);
  assert.equal(progressPercent('restart', true), 100);
});

test('didn’t install: the backup line only when a backup was kept', () => {
  const at = new Date(2026, 8, 26, 9, 41);
  assert.match(failedDetails('FT-UPD-03', at, true, 'en-US'), /^Code FT-UPD-03 · Sep 26, 2026, 9:41\sAM\. The backup was kept\.$/);
  assert.match(failedDetails('FT-UPD-01', at, false, 'en-US'), /^Code FT-UPD-01 · Sep 26, 2026, 9:41\sAM\.$/);
  assert.match(FAILED.body('Sam'), /let Sam know\.$/);
});

test('needs help: the reason goes under Details', () => {
  assert.equal(helpDetails('FT-UPD-HELP-SPACE', 'disk_space', '1.4.0'), 'Code FT-UPD-HELP-SPACE · update 1.4 needs more free space on this computer.');
});

test('picked files: red for a bad file, neutral for older/same/failed', () => {
  const sig = checkCopy({ result: 'rejected', reason: 'signature', code: 'FT-UPD-SIG', file_name: 'FinTrack-1.4.ftupdate' }, 'Sam');
  assert.equal(sig.tone, 'danger');
  assert.equal(sig.title, 'Don’t install this file');
  assert.equal(sig.details, 'FinTrack-1.4.ftupdate · the signature check failed (FT-UPD-SIG)');
  const dmg = checkCopy({ result: 'rejected', reason: 'damaged', code: 'FT-UPD-DMG', file_name: 'x.ftupdate' }, 'Sam');
  assert.equal(dmg.body, 'This file is damaged or incomplete. Don’t install it. Ask Sam to send it again.');
  const old = checkCopy({ result: 'older', code: 'FT-UPD-OLD', file_name: 'x.ftupdate', file_version: '1.2.0', current_version: '1.4.0' }, 'Sam');
  assert.equal(old.tone, 'neutral');
  assert.equal(old.body, 'This file is Iron Owl 1.2; you have 1.4. Nothing was installed. You can delete the file.');
  const same = checkCopy({ result: 'same', code: 'FT-UPD-SAME', file_name: 'x.ftupdate', file_version: '1.4.0', current_version: '1.4.0' }, null);
  assert.equal(same.body, 'Iron Owl is already on version 1.4. You can delete the file.');
  const failed = checkCopy({ result: 'failed', code: 'FT-UPD-03', file_name: 'x.ftupdate', file_version: '1.4.0', current_version: '1.3.0' }, 'Sam');
  assert.equal(failed.body, 'This update didn’t install before, so Iron Owl won’t try it again. Ask Sam for a new file.');
});

test('Settings words', () => {
  assert.equal(ABOUT.ready('1.4.0'), 'Version 1.4 is ready to install');
  assert.equal(ABOUT.needsHelp('2.0.0', 'Sam'), 'Version 2.0 needs Sam’s help to install');
  assert.equal(ABOUT.notInstalled(null), 'This copy of Iron Owl can’t install updates by itself. Ask the person who set up Iron Owl.');
  assert.match(ABOUT.explain('Sam'), /^Sam sends updates by email/);
  const now = new Date(2026, 9, 4, 15, 0);
  assert.equal(lastUpdateText(new Date(2026, 9, 4, 8, 0).toISOString(), now), 'Today');
  assert.equal(lastUpdateText(new Date(2026, 9, 3, 23, 0).toISOString(), now), 'Yesterday');
  assert.equal(lastUpdateText(null, now), 'Not yet');
});

test('no server, port, checksum or path words anywhere the user reads', () => {
  const texts = [
    FAILED.title, FAILED.body('Sam'), HELP.title('Sam'), HELP.body('Sam'), ABOUT.gitText, ABOUT.explain('Sam'),
    ABOUT.notInstalled('Sam'), BANNER.lead, BANNER.remindTitle, BANNER.remindBody,
  ];
  for (const t of texts) assert.doesNotMatch(t, /server|port\b|checksum|sha|C:\|\//i, t);
});

test('a failed progress poll is a restart only if Iron Owl is gone or new', () => {
  // no answer from /api/health, or a new boot id: restarting
  assert.equal(afterPollError('a', null, false), 'restarting');
  assert.equal(afterPollError('a', { boot_id: 'b' }, true), 'restarting');
  // no boot id from before: a restart can't be ruled out
  assert.equal(afterPollError(null, { boot_id: 'a' }, true), 'restarting');
  // the same FinTrack still answers: nothing restarted
  assert.equal(afterPollError('a', { boot_id: 'a' }, false), 'retry');
  assert.equal(afterPollError('a', { boot_id: 'a' }, true), 'signIn');
});

test('waiting for the restart: back on a new boot id, FT-UPD-04 once after 180 s', () => {
  assert.equal(RESTART_LIMIT_MS, 180_000);
  assert.equal(restartedBack('a', { boot_id: 'b' }, false), true);
  assert.equal(restartedBack('a', { boot_id: 'a' }, true), false);
  assert.equal(restartedBack('a', null, true), false);
  assert.equal(restartedBack(null, { boot_id: 'a' }, false), false);
  assert.equal(restartedBack(null, { boot_id: 'a' }, true), true);
  assert.equal(restartOverdue(0, 179_000, false), false);
  assert.equal(restartOverdue(0, 181_000, false), true);
  assert.equal(restartOverdue(0, 400_000, true), false); // shown once; polling goes on
});

test('Iron Owl from GitHub: banner words and the Settings card', () => {
  assert.equal(bannerLead('github'), 'A new version of Iron Owl is ready to install.');
  assert.equal(bannerLead('file'), BANNER.lead);
  assert.equal(bannerLead(undefined), BANNER.lead);
  assert.equal(bannerAnnounce('github', '2.1.0'), 'A new version of Iron Owl is ready to install: version 2.1.');
  assert.equal(bannerAnnounce('file', '1.4.0'), BANNER.announce('1.4.0'));
  const now = new Date(2026, 9, 4, 12, 0);
  assert.equal(GITHUB.lastChecked(null, now), 'Last checked: Not yet');
  assert.equal(GITHUB.lastChecked(new Date(2026, 9, 4, 8, 0).toISOString(), now), 'Last checked: Today');
  assert.match(GITHUB.autoOn(false), /Check now/);
  for (const text of [GITHUB.explain, GITHUB.privacy, GITHUB.auto, SOURCE_OFF, GITHUB.failed]) {
    assert.doesNotMatch(text, /FT-|http|server|API|FinTrack/);
  }
});
