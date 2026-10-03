/**
 * Unit tests for when a Modal closes on a backdrop click (src/components/backdropClose.ts).
 * Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BACKDROP_GRACE_MS, shouldCloseOnBackdrop } from '../src/components/backdropClose.ts';

const later = BACKDROP_GRACE_MS + 50;

test('a normal click on the backdrop closes it', () => {
  assert.equal(shouldCloseOnBackdrop({ pressOnBackdrop: true, releaseOnBackdrop: true, openedAt: 1000, now: 1000 + later }), true);
});

test('the extra clicks of the double-click that opened it do not close it', () => {
  assert.equal(shouldCloseOnBackdrop({ pressOnBackdrop: true, releaseOnBackdrop: true, openedAt: 1000, now: 1120 }), false);
  assert.equal(shouldCloseOnBackdrop({ pressOnBackdrop: true, releaseOnBackdrop: true, openedAt: 1000, now: 1000 + BACKDROP_GRACE_MS }), true);
});

test('a drag from inside the window out to the backdrop does not close it', () => {
  assert.equal(shouldCloseOnBackdrop({ pressOnBackdrop: false, releaseOnBackdrop: true, openedAt: 0, now: later }), false);
  assert.equal(shouldCloseOnBackdrop({ pressOnBackdrop: true, releaseOnBackdrop: false, openedAt: 0, now: later }), false);
});
