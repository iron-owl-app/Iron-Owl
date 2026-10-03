import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev: Vite on 127.0.0.1:5173, /api proxied to the FastAPI backend.
// Prod: the backend serves frontend/dist at "/", so everything is one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
    // The Help page imports ../HELP.md (repo root) with ?raw. Setting `allow` replaces Vite's
    // default (this folder), so list this folder and that one file; nothing else above it.
    fs: {
      allow: ['.', '../HELP.md'],
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    // Served from localhost only; one ~190 kB-gzip bundle (React + Recharts) is fine.
    chunkSizeWarningLimit: 800,
    // Never inline fonts as data: URIs: the backend's CSP has no font-src data:.
    assetsInlineLimit: (file) => (/\.(woff2?|ttf|otf)$/i.test(file) ? false : undefined),
  },
});
