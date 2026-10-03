import { preloadable } from '../lib/preload';

/** The Help page's code, loaded early by the Gate so Help also opens on the "isn't running" screen. */
export const helpModule = preloadable(() => import('./Help'));
