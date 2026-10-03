/**
 * Unit tests for src/lib/preload.ts (Help's code loaded early, so Help opens on the
 * "isn't running" screen). Run with `npm run test:unit`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { preloadable } from '../src/lib/preload.ts';

test('loads once and says when it is ready', async () => {
  let calls = 0;
  const mod = preloadable(async () => {
    calls += 1;
    return { page: 'help' };
  });
  assert.equal(mod.isReady(), false);
  const [a, b] = await Promise.all([mod.load(), mod.load()]);
  assert.equal(a, b);
  assert.equal(calls, 1);
  assert.equal(mod.isReady(), true);
  await mod.load();
  assert.equal(calls, 1);
});

test('a failed load is not ready and is tried again', async () => {
  let calls = 0;
  const mod = preloadable(async () => {
    calls += 1;
    if (calls === 1) throw new Error('server stopped');
    return 'ok';
  });
  await assert.rejects(mod.load(), /server stopped/);
  assert.equal(mod.isReady(), false);
  assert.equal(await mod.load(), 'ok');
  assert.equal(calls, 2);
  assert.equal(mod.isReady(), true);
});
