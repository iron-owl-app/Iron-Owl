/**
 * When a click on a <dialog>'s backdrop may close it (Modal). Pure, for scripts/modalBackdrop.test.ts.
 * - Both the press and the release must be on the backdrop itself: a drag that starts inside the
 *   window (selecting text) and ends outside doesn't close it.
 * - Clicks in the first moments after opening are ignored: the extra clicks of a double- or
 *   triple-click that opened it (the calendar's + or a day) land outside and would close it at once.
 */
export const BACKDROP_GRACE_MS = 300;

export function shouldCloseOnBackdrop(s: { pressOnBackdrop: boolean; releaseOnBackdrop: boolean; openedAt: number; now: number }): boolean {
  return s.pressOnBackdrop && s.releaseOnBackdrop && s.now - s.openedAt >= BACKDROP_GRACE_MS;
}
