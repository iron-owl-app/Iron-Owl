import { useEffect, useState } from 'react';
import { OwlLogo } from '../OwlLogo';

/** After this long without an answer, "Starting FinTrack…" becomes "Opening your data…". */
const OPENING_AFTER_MS = 300;

/**
 * State 1, "Starting FinTrack…" (design D4). Same markup and classes as the static screen in
 * index.html (styled by public/boot.css), so React taking over doesn't flicker. The first status
 * read gives up after 20 s (state.tsx), which then shows "FinTrack isn't running".
 */
export function StartingScreen() {
  const [opening, setOpening] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setOpening(true), OPENING_AFTER_MS);
    return () => window.clearTimeout(t);
  }, []);

  return (
    <div className="ft-boot">
      <OwlLogo className="ft-boot-logo" />
      <div role="status" aria-live="polite">
        <p className="ft-boot-name">Iron Owl</p>
        <p className="ft-boot-msg">{opening ? 'Opening your data…' : 'Starting Iron Owl…'}</p>
      </div>
      <div className="ft-boot-bar" aria-hidden="true">
        <span />
      </div>
      <p className="ft-boot-hint">This usually takes a few seconds.</p>
    </div>
  );
}
