/**
 * Unit tests for src/lib/recoveryCode.ts. Run with `npm run test:unit`
 * (node --test; Node 22.18+/24 runs erasable TypeScript directly, no build step).
 *
 * The Damm/group vectors are shared with the backend (backend/tests/test_recovery.py) in
 * shared/recovery-code-vectors.json at the repo root, so both sides compute the same checks.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  CODE_LEN,
  badGroups,
  cleanDigits,
  damm,
  distribute,
  formatGroup,
  fullGroups,
  groupCheck,
  groupOk,
  spokenDigits,
  typoMessage,
} from '../src/lib/recoveryCode.ts';

type Raw = Record<string, unknown>;
const raw = JSON.parse(readFileSync(new URL('../../shared/recovery-code-vectors.json', import.meta.url), 'utf8')) as Raw;

/** Accepts {input, check} objects or [input, check] pairs (the file is shared with pytest). */
function dammVectors(): [string, number][] {
  const list = (raw.damm ?? []) as unknown[];
  return list.map((v) => {
    if (Array.isArray(v)) return [String(v[0]), Number(v[1])];
    const o = v as Raw;
    return [String(o.input ?? o.digits ?? o.s), Number(o.check ?? o.expected ?? o.digit)];
  });
}

/** Accepts {index, data, group} objects or [index, data, group] triples. */
function groupVectors(): [number, string, string][] {
  const list = (raw.groups ?? []) as unknown[];
  return list.map((v) => {
    if (Array.isArray(v)) return [Number(v[0]), String(v[1]), String(v[2])];
    const o = v as Raw;
    return [Number(o.index ?? o.group_index), String(o.data ?? o.data5), String(o.group ?? o.group6 ?? o.expected)];
  });
}

const CODE = ['739151', '204868', '581233', '916405', '362581', '047913'];
const EMPTY = ['', '', '', '', '', ''];

test('shared Damm vectors', () => {
  const vs = dammVectors();
  assert.ok(vs.length >= 4, 'expected Damm vectors in shared/recovery-code-vectors.json');
  for (const [input, check] of vs) assert.equal(damm(input), check, `damm(${input})`);
});

test('shared group vectors (index is 1-based)', () => {
  const vs = groupVectors();
  assert.equal(vs.length, 6);
  for (const [index, data, group] of vs) {
    assert.equal(String(groupCheck(index, data)), group.slice(5), `group ${index}`);
    assert.equal(data + String(groupCheck(index, data)), group);
    assert.ok(groupOk(index, group));
  }
});

test('Damm catches every single-digit change and adjacent swap in a group', () => {
  CODE.forEach((g, i) => {
    for (let p = 0; p < 6; p++) {
      for (let d = 0; d <= 9; d++) {
        if (String(d) === g[p]) continue;
        const bad = g.slice(0, p) + d + g.slice(p + 1);
        assert.equal(groupOk(i + 1, bad), false, `${bad} in group ${i + 1}`);
      }
      if (p < 5 && g[p] !== g[p + 1]) {
        const swapped = g.slice(0, p) + g[p + 1] + g[p] + g.slice(p + 2);
        assert.equal(groupOk(i + 1, swapped), false, `swap ${swapped}`);
      }
    }
  });
});

test('a group in the wrong position is caught', () => {
  assert.deepEqual(badGroups([CODE[1]!, CODE[0]!, ...CODE.slice(2)]), [1, 2]);
});

test('groupOk rejects non-digits and wrong lengths', () => {
  assert.equal(groupOk(1, '73915'), false);
  assert.equal(groupOk(1, '7391510'), false);
  assert.equal(groupOk(1, '73915a'), false);
  assert.equal(groupOk(1, '７３９１５１'), false); // full-width digits
  assert.throws(() => damm('12a'));
});

test('cleanDigits keeps ASCII digits only', () => {
  assert.equal(cleanDigits('739 151-204 868'), '739151204868');
  assert.equal(cleanDigits('７３９'), '');
});

test('distribute: 7+ digits fill from the current box', () => {
  const r = distribute(EMPTY, 2, '581233 916405 3');
  assert.deepEqual(r.boxes, ['', '', '581233', '916405', '3', '']);
  assert.equal(r.focus, 4);
});

test('distribute: exactly 36 digits fill from group 1 wherever pasted', () => {
  const pasted = CODE.map(formatGroup).join('  ');
  const r = distribute(['1', '', '99', '', '', ''], 3, pasted);
  assert.deepEqual(r.boxes, CODE);
  assert.equal(r.focus, 5);
  assert.equal(cleanDigits(pasted).length, CODE_LEN);
});

test('distribute: separators ignored, extra digits past box 6 dropped, later boxes kept', () => {
  const r = distribute(['', '', '', '', '', '123'], 3, '916-405-362-581-047-913-555');
  assert.deepEqual(r.boxes, ['', '', '', '916405', '362581', '047913']);
  const keep = distribute(['', '', '', '', '', '047913'], 0, '739151-2048');
  assert.deepEqual(keep.boxes, ['739151', '2048', '', '', '', '047913']);
  assert.equal(keep.focus, 1);
});

test('badGroups / fullGroups', () => {
  const typo = [...CODE];
  typo[3] = '916415';
  typo[1] = '204869';
  assert.deepEqual(badGroups(typo), [2, 4]);
  assert.deepEqual(badGroups(['7391', ...CODE.slice(1)]), []);
  assert.equal(fullGroups(['7391', ...CODE.slice(1)]), 5);
});

test('typoMessage plurals', () => {
  assert.equal(typoMessage([]), '');
  assert.equal(typoMessage([4]), 'Group 4 has a typo.');
  assert.equal(typoMessage([2, 4]), 'Groups 2 and 4 have a typo.');
  assert.equal(typoMessage([1, 2, 4]), 'Groups 1, 2 and 4 have a typo.');
});

test('formatGroup / spokenDigits', () => {
  assert.equal(formatGroup('739151'), '739 151');
  assert.equal(spokenDigits('739151'), '7 3 9 1 5 1');
});
