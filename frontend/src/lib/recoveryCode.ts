/**
 * Recovery sheet code helpers (MAPPING §2, §5.1). Pure and import-free, written in
 * erasable TypeScript so `node --test scripts/recoveryCode.test.ts` can run it directly.
 *
 * A code is 6 groups of 6 digits. Digits 1-5 of each group are random; digit 6 is a Damm
 * check digit over the group's number (1-6) followed by its 5 data digits, so the app can
 * say which group has a typo without asking the server. The backend (app/recovery.py)
 * computes the same thing; both are tested against shared/recovery-code-vectors.json.
 *
 * Nothing here logs, stores or copies a code.
 */

export const GROUPS = 6;
export const GROUP_LEN = 6;
export const CODE_LEN = GROUPS * GROUP_LEN;

/** The Damm quasigroup table (weakly totally anti-symmetric, order 10). */
export const DAMM: readonly (readonly number[])[] = [
  [0, 3, 1, 7, 5, 9, 8, 6, 4, 2],
  [7, 0, 9, 2, 1, 5, 4, 8, 6, 3],
  [4, 2, 0, 6, 8, 7, 1, 3, 5, 9],
  [1, 7, 5, 0, 9, 8, 3, 4, 2, 6],
  [6, 1, 2, 3, 0, 4, 5, 9, 7, 8],
  [3, 6, 7, 4, 2, 0, 9, 5, 8, 1],
  [5, 8, 6, 9, 7, 2, 0, 1, 3, 4],
  [8, 9, 4, 5, 3, 6, 2, 0, 1, 7],
  [9, 4, 3, 8, 6, 1, 7, 2, 0, 5],
  [2, 5, 8, 1, 4, 3, 6, 7, 9, 0],
];

/** Damm check digit of an ASCII digit string (0 for "" ; a string ending in its check digit gives 0). */
export function damm(digits: string): number {
  let interim = 0;
  for (const ch of digits) {
    const d = ch.charCodeAt(0) - 48;
    if (d < 0 || d > 9) throw new RangeError('damm: digits only');
    interim = DAMM[interim]![d]!;
  }
  return interim;
}

/** Check digit for group `index` (1-based) with 5 data digits. */
export function groupCheck(index: number, data5: string): number {
  return damm(String(index) + data5);
}

const SIX_DIGITS = /^[0-9]{6}$/;

/** Whether a complete 6-digit group `g` is right for its position `index` (1-based). */
export function groupOk(index: number, g: string): boolean {
  if (!SIX_DIGITS.test(g)) return false;
  return groupCheck(index, g.slice(0, 5)) === g.charCodeAt(5) - 48;
}

/** Only the ASCII digits 0-9 of `s` (spaces, dashes and anything else are dropped). */
export function cleanDigits(s: string): string {
  return s.replace(/[^0-9]/g, '');
}

/**
 * Put pasted (or overflowing) digits into the boxes. 7 or more digits fill box `start` and the
 * ones after it; exactly 36 digits always fill from box 1, wherever they were pasted. Boxes
 * past the digits keep what they had. Returns a new array and the box to focus next.
 */
export function distribute(boxes: readonly string[], start: number, raw: string): { boxes: string[]; focus: number } {
  const digits = cleanDigits(raw);
  const from = digits.length === CODE_LEN ? 0 : Math.max(0, Math.min(GROUPS - 1, start));
  const next = boxes.slice(0, GROUPS);
  while (next.length < GROUPS) next.push('');
  const usable = digits.slice(0, (GROUPS - from) * GROUP_LEN);
  let last = from;
  for (let k = 0; k * GROUP_LEN < usable.length; k++) {
    next[from + k] = usable.slice(k * GROUP_LEN, (k + 1) * GROUP_LEN);
    last = from + k;
  }
  const lastFull = next[last]!.length === GROUP_LEN;
  return { boxes: next, focus: lastFull && last < GROUPS - 1 ? last + 1 : last };
}

/** 1-based numbers of the complete groups whose check digit is wrong. */
export function badGroups(boxes: readonly string[]): number[] {
  const out: number[] = [];
  boxes.forEach((g, i) => {
    if (g.length === GROUP_LEN && !groupOk(i + 1, g)) out.push(i + 1);
  });
  return out;
}

/** How many boxes hold a full group. */
export function fullGroups(boxes: readonly string[]): number {
  return boxes.filter((g) => g.length === GROUP_LEN).length;
}

/** "739151" → "739 151" (screen and print). */
export function formatGroup(g: string): string {
  return g.length > 3 ? `${g.slice(0, 3)} ${g.slice(3)}` : g;
}

/** "739151" → "7 3 9 1 5 1" so screen readers say each digit. */
export function spokenDigits(g: string): string {
  return g.split('').join(' ');
}

/** "Group 4 has a typo." · "Groups 2 and 4 have a typo." · "Groups 1, 2 and 4 have a typo." */
export function typoMessage(groups: readonly number[]): string {
  if (groups.length === 0) return '';
  if (groups.length === 1) return `Group ${groups[0]} has a typo.`;
  const head = groups.slice(0, -1).join(', ');
  return `Groups ${head} and ${groups[groups.length - 1]} have a typo.`;
}
