/**
 * Unit tests for src/components/gate/gateCopy.ts (the app-window screens' words, MAPPING copy
 * table). Run with `npm run test:unit` (node --test runs erasable TypeScript directly).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  AFTER_WAIT,
  ELSEWHERE,
  detailsLine,
  greeting,
  lockoutLine,
  passwordCopy,
  reasonFromServer,
  waitButton,
  waitWords,
  wrongPasswordLine,
} from '../src/components/gate/gateCopy.ts';

const at = (h: number) => new Date(2026, 8, 26, h, 14);

test('greeting follows the time of day', () => {
  assert.equal(greeting(at(0)), 'Good morning');
  assert.equal(greeting(at(11)), 'Good morning');
  assert.equal(greeting(at(12)), 'Good afternoon');
  assert.equal(greeting(at(17)), 'Good afternoon');
  assert.equal(greeting(at(18)), 'Good evening');
  assert.equal(greeting(at(23)), 'Good evening');
});

test('server lock reasons map to screen reasons', () => {
  assert.equal(reasonFromServer(null), 'start');
  assert.equal(reasonFromServer(undefined, 'ended'), 'ended');
  assert.equal(reasonFromServer('idle'), 'idle');
  assert.equal(reasonFromServer('closed'), 'closed');
  assert.equal(reasonFromServer('manual'), 'manual');
  assert.equal(reasonFromServer('moved'), 'ended');
  assert.equal(reasonFromServer('shutdown'), 'ended');
});

test('sign in and locked copy', () => {
  const start = passwordCopy('start', 15, at(9));
  assert.equal(start.title, 'Good morning');
  assert.equal(start.body, 'Enter your Iron Owl password to open your data.');
  assert.equal(start.button, 'Open Iron Owl');
  assert.equal(start.busy, 'Opening Iron Owl…');
  assert.equal(start.icon, 'logo');

  const idle = passwordCopy('idle', 15);
  assert.equal(idle.title, 'Iron Owl is locked');
  assert.equal(
    idle.body,
    'You were away for 15 minutes, so Iron Owl locked itself to keep your data safe. Enter your password to pick up where you left off.',
  );
  assert.match(passwordCopy('idle', 1).body, /^You were away for 1 minute, so/);
  assert.equal(passwordCopy('closed', 15).body, 'Iron Owl was closed. Enter your password to open it again.');
  assert.equal(passwordCopy('manual', 15).body, 'You locked Iron Owl. Enter your password to pick up where you left off.');
  assert.equal(
    passwordCopy('ended', 15).body,
    'Iron Owl locked itself to keep your data safe. Enter your password to pick up where you left off.',
  );
  const here = passwordCopy('here', 15);
  assert.equal(here.title, 'Open Iron Owl here');
  assert.equal(here.body, 'Enter your password to open Iron Owl in this window. The other window will lock.');
});

test('wrong password and waits', () => {
  assert.equal(wrongPasswordLine(3), 'That password isn’t right. You have 3 tries left before a short wait.');
  assert.equal(wrongPasswordLine(1), 'That password isn’t right. You have 1 try left before a short wait.');
  assert.equal(wrongPasswordLine(null), 'That password isn’t right. Try again.');
  assert.equal(lockoutLine(30), 'Too many tries. Please wait 30 seconds, then try again.');
  assert.equal(lockoutLine(120), 'Too many tries. Please wait 2 minutes, then try again.');
  assert.equal(waitWords(60), '1 minute');
  assert.equal(waitWords(90), '1 minute 30 seconds');
  assert.equal(waitWords(900), '15 minutes');
  assert.equal(waitButton(29), 'Please wait 29s');
  assert.equal(waitButton(118), 'Please wait 1:58');
  assert.equal(AFTER_WAIT, 'You can try again now. Another wrong try means a longer wait.');
});

test('details line names the contact, or says what to do without one', () => {
  const when = new Date(2026, 8, 26, 9, 14);
  assert.match(detailsLine('FT-START-02', when, 'Sam', 'en-US'), /^Code FT-START-02 · Sep 26, 2026, 9:14\s?AM\. If you call Sam, read them this code\.$/);
  assert.match(detailsLine('FT-RUN-01', when, null, 'en-US'), /If you ask for help, read out this code\.$/);
});

test('already open copy', () => {
  assert.equal(ELSEWHERE.second.title, 'Iron Owl is already open in another window');
  assert.equal(ELSEWHERE.moved.body, 'You opened Iron Owl in another window, so this one locked. You can close this window.');
});
