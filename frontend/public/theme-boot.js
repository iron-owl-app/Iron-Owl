/*
 * Theme switch (Release 3.16): puts the saved theme on <html data-theme> before the first
 * paint, so a dark or Warm night choice never flashes light (and the reverse). A plain file,
 * not inline: the CSP allows only same-origin scripts. Same rules as applyTheme in
 * src/lib/themeCore.ts (scripts/theme.test.ts runs both and compares). Nothing saved, or
 * anything unexpected: Match Windows (no attribute).
 */
(function () {
  var COLORS = { light: '#f7f6f2', dark: '#0b0e14', warm: '#140e09' };
  try {
    var v = JSON.parse(window.localStorage.getItem('fintrack.ui.theme'));
    if (v !== 'light' && v !== 'dark' && v !== 'warm') return;
    document.documentElement.setAttribute('data-theme', v);
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i++) metas[i].setAttribute('content', COLORS[v]);
  } catch (e) {
    /* storage blocked or bad JSON: Match Windows */
  }
})();
