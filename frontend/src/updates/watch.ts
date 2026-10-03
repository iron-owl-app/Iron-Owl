/**
 * Pure decisions for following an install (UpdateProvider), kept apart so they can be unit
 * tested (`npm run test:unit`).
 */

/** No answer this long after the restart began: "FinTrack isn't running" (FT-UPD-04). */
export const RESTART_LIMIT_MS = 180_000;
/** Health polls while waiting for the restart. */
export const HEALTH_POLL_MS = 1_500;
/** After FT-UPD-04 is shown: keep asking, slower, and carry on by itself if FinTrack comes back. */
export const HEALTH_POLL_AFTER_LIMIT_MS = 5_000;

/**
 * A progress poll got a 401 or no answer. Before assuming FinTrack is restarting, the window
 * asks /api/health: only an unreachable FinTrack, or a different one (a new boot id), means a
 * restart. The same FinTrack still answering means nothing restarted:
 *   - `retry`: a passing hiccup (a slow answer): ask for the progress again;
 *   - `signIn`: this window's session ended (locked, or taken by another window): sign in again,
 *     then the status says where the install is.
 * Without a boot id from before the install (`boot0` null) a restart can't be ruled out.
 */
export type PollErrorAction = 'restarting' | 'retry' | 'signIn';

export function afterPollError(
  boot0: string | null,
  health: { boot_id: string } | null,
  unauthorized: boolean,
): PollErrorAction {
  if (!health || boot0 === null || health.boot_id !== boot0) return 'restarting';
  return unauthorized ? 'signIn' : 'retry';
}

/** Is FinTrack back after the restart? A new boot id (or, without one, an answer after it was down). */
export function restartedBack(boot0: string | null, health: { boot_id: string } | null, sawDown: boolean): boolean {
  if (!health) return false;
  return boot0 !== null ? health.boot_id !== boot0 : sawDown;
}

/** Show FT-UPD-04 now? Once, when the wait has run past the limit. */
export function restartOverdue(since: number, now: number, alreadyShown: boolean): boolean {
  return !alreadyShown && now - since > RESTART_LIMIT_MS;
}
