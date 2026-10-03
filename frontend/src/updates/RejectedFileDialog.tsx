import { useId, useRef } from 'react';
import { useUpdates } from './UpdateProvider';
import { UpdDialog } from './UpdDialog';
import { checkCopy } from './copy';
import { CodeText } from './CodeText';

/**
 * A file the user picked that won't be installed (design D5 state 8). Red "Don't install this file"
 * (an alert dialog, the one place this flow uses red) for a bad signature, not an update, a
 * damaged or an oversized file; a neutral note for an older or the same version, or a file
 * that already failed to install. OK has focus.
 */
export function RejectedFileDialog() {
  const { flow, closeCheck, contact } = useUpdates();
  const titleId = useId();
  const bodyId = useId();
  const okRef = useRef<HTMLButtonElement>(null);
  const open = flow.kind === 'check';
  const c = flow.kind === 'check' ? checkCopy(flow.check, contact) : null;
  const danger = c?.tone === 'danger';

  return (
    <UpdDialog
      open={open}
      role={danger ? 'alertdialog' : 'dialog'}
      labelledBy={titleId}
      describedBy={bodyId}
      className={`upd-check-dialog${danger ? ' is-danger' : ''}`}
      onEscape={closeCheck}
      initialFocus={okRef}
    >
      {c && (
        <div className="upd-dialog-inner">
          <span className={`upd-ic52 ${danger ? 'is-danger' : 'is-info'}`} aria-hidden="true">
            {danger ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3 3 20h18Z" />
                <path d="M12 10v4M12 17v.5" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 11v6M12 7.5v.5" />
              </svg>
            )}
          </span>
          <div>
            <h2 id={titleId} className="upd-dialog-title">
              {c.title}
            </h2>
            <p id={bodyId} className="upd-dialog-body">
              {c.body}
            </p>
          </div>
          {c.note && <p className="upd-dialog-note">{c.note}</p>}
          <details className="upd-details">
            <summary>Details</summary>
            <p>
              <CodeText text={c.details} />
            </p>
          </details>
          <div className="upd-dialog-actions">
            <button ref={okRef} type="button" className="upd-btn upd-btn-52 upd-btn-strong upd-btn-wide" onClick={closeCheck}>
              OK
            </button>
          </div>
        </div>
      )}
    </UpdDialog>
  );
}
