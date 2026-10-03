import { applyTheme, isThemeChoice } from '../lib/themeCore';

/**
 * Mock-only theme preview: `?theme=light|dark|warm` (or `system`) shows that theme without
 * saving it, whatever this browser has saved or Windows uses. It sets `<html data-theme>` the
 * way the real switch does (lib/themeCore.ts). Picking a theme in the UI still works (and
 * saves) as usual. Production never imports this file.
 */
export function installMockTheme(search: string = window.location.search): void {
  const theme = new URLSearchParams(search).get('theme');
  if (isThemeChoice(theme)) applyTheme(theme);
}
