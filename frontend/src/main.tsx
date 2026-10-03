import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { initTextSize } from './lib/textSize';
import { initTheme } from './lib/theme';
// IBM Plex Sans for Home's big numbers (--font-num): latin 500/600/700 only, served as files (the CSP has no font-src data:).
import '@fontsource/ibm-plex-sans/latin-500.css';
import '@fontsource/ibm-plex-sans/latin-600.css';
import '@fontsource/ibm-plex-sans/latin-700.css';
import './styles.css';
import './styles-a.css';
import './styles-b.css';

// Settings › Text size: set the root font size before anything renders (the lock screen too).
initTextSize();
// Colors (Match Windows / Light / Dark / Warm night): public/theme-boot.js set it before the first paint; this keeps it and follows other windows.
initTheme();

async function boot() {
  // Dev-only in-memory API stub for UI work without the backend.
  // `import.meta.env.DEV` is statically false in production builds, so this
  // branch — and the mock module — are dropped from the bundle entirely.
  if (import.meta.env.DEV && (import.meta.env.VITE_MOCK === '1' || import.meta.env.MODE === 'mock')) {
    const { installMock } = await import('./dev/mock');
    installMock();
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void boot();
