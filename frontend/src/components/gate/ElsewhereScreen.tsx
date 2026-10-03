import { useEffect, useId, useRef, useState } from 'react';
import { useApp } from '../../state';
import { postTabs, requestHandoff } from '../../lib/tabChannel';
import { GateCard } from './GateCard';
import { ELSEWHERE } from './gateCopy';

/**
 * State 4, "already open in another window" (design D4, MAPPING conflict 3).
 *
 * - `second`: this window opened while another has FinTrack open. Open here / Use the other window.
 * - `moved`: this window had FinTrack open, then another window took it over. Open here.
 *
 * Open here asks the other window to hand its session over (it locks itself); if no window
 * answers within 1.5 s, the password screen asks for the password instead ("Open FinTrack here").
 */
export function ElsewhereScreen() {
  const { elsewhere, adoptSession, showPasswordHere } = useApp();
  const copy = ELSEWHERE[elsewhere];
  const titleId = useId();
  const [note, setNote] = useState('');
  const [opening, setOpening] = useState(false);
  const mainRef = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    mainRef.current?.focus();
    return () => {
      alive.current = false;
    };
  }, [elsewhere]);

  async function openHere() {
    if (opening) return;
    setOpening(true);
    setNote(ELSEWHERE.opening);
    const token = await requestHandoff();
    if (!alive.current) return;
    if (token) adoptSession(token);
    else showPasswordHere();
  }

  function chooseOther() {
    postTabs({ type: 'focus-request' });
    // Works when this window was opened on its own (the FinTrack window); a browser tab stays open.
    window.close();
    window.setTimeout(() => {
      if (alive.current && !window.closed) setNote(ELSEWHERE.useOther);
    }, 150);
  }

  return (
    <GateCard width={540} icon="windows" title={copy.title} titleId={titleId}>
      <p className="gate-body">{copy.body}</p>
      <div className="gate-actions">
        <button
          ref={mainRef}
          type="button"
          className="btn btn-primary btn-xl gate-main-btn"
          onClick={() => void openHere()}
          aria-disabled={opening ? true : undefined}
        >
          Open here
        </button>
        {elsewhere === 'second' && (
          <button type="button" className="btn btn-xl gate-second-btn" onClick={chooseOther} disabled={opening}>
            Use the other window
          </button>
        )}
      </div>
      <p className="gate-status" role="status">
        {note}
      </p>
    </GateCard>
  );
}
