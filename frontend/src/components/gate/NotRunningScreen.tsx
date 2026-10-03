import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api';
import { useApp } from '../../state';
import { GateCard } from './GateCard';
import { detailsLine } from './gateCopy';
import { CodeText } from '../../updates/CodeText';
import { helpModule } from '../../pages/helpModule';

/**
 * State 3, "FinTrack isn't running" (design D4). Replaces the old technical message: no
 * backend, server, port or address, only a short code under Details for whoever the user calls.
 *
 * Try again asks FinTrack whether it's answering (5 s); if it is, the window reloads.
 * Help shows only when its code was loaded before the server stopped (the Gate loads it early).
 */
export function NotRunningScreen() {
  const { unreachable, authInfo } = useApp();
  const titleId = useId();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const code = unreachable?.code ?? 'FT-START-02';
  const at = unreachable?.at ?? new Date();

  useEffect(() => {
    btnRef.current?.focus();
  }, []);

  async function retry() {
    if (busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await api.app.health();
      window.location.reload();
    } catch {
      setBusy(false);
      setFailed(true);
      window.requestAnimationFrame(() => btnRef.current?.focus());
    }
  }

  return (
    <GateCard width={520} icon="stopped" title="Iron Owl isn’t running" titleId={titleId}>
      <p className="gate-body">Close this window and open Iron Owl again from the desktop icon. Your data is safe.</p>
      <div className="gate-actions">
        <button
          ref={btnRef}
          type="button"
          className="btn btn-primary btn-xl gate-main-btn"
          onClick={() => void retry()}
          aria-disabled={busy ? true : undefined}
        >
          {busy ? 'Checking…' : 'Try again'}
        </button>
        <p className="gate-warn-text" role="status">
          {failed ? 'Still not running. Please close this window and use the desktop icon.' : ''}
        </p>
      </div>
      {helpModule.isReady() && (
        <Link className="link-btn gate-link gate-help-link" to="/help">
          Help
        </Link>
      )}
      <details className="gate-details">
        <summary>Details</summary>
        <p>
          <CodeText text={detailsLine(code, at, authInfo.supportContact)} />
        </p>
      </details>
    </GateCard>
  );
}
