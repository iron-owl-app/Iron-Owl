import { useId } from 'react';
import { useUpdates } from './UpdateProvider';
import { UpdDialog } from './UpdDialog';
import { NEWS } from './copy';

/**
 * What's new in FinTrack 1.4 (design D5 state 2): 560px, no × (Esc = Later). The only place
 * an install starts. Notes come from the update file and are shown as plain text.
 */
export function WhatsNewDialog() {
  const { flow, closeNews, install, starting } = useUpdates();
  const titleId = useId();
  const infoId = useId();
  const open = flow.kind === 'news';
  const offer = flow.kind === 'news' ? flow.offer : null;

  return (
    <UpdDialog open={open} labelledBy={titleId} describedBy={infoId} className="upd-news" onEscape={closeNews} busy={starting}>
      {offer && (
        <div className="upd-dialog-inner">
          <h2 id={titleId} className="upd-dialog-title upd-news-title">
            {NEWS.title(offer.version)}
          </h2>
          {offer.notes.length > 0 && (
            <ul className="upd-news-list">
              {offer.notes.slice(0, 6).map((n, i) => (
                <li key={i}>
                  <span className="upd-check" aria-hidden="true">
                    ✓
                  </span>
                  <span>{n}</span>
                </li>
              ))}
            </ul>
          )}
          <p id={infoId} className="upd-info">
            {NEWS.info(offer.minutes)}
          </p>
          <div className="upd-dialog-actions">
            <button type="button" className="upd-btn upd-btn-52 upd-btn-outline" onClick={closeNews} disabled={starting}>
              {NEWS.later}
            </button>
            <button
              type="button"
              className="upd-btn upd-btn-52 upd-btn-primary"
              onClick={install}
              aria-disabled={starting ? true : undefined}
            >
              {starting ? NEWS.starting : NEWS.install}
            </button>
          </div>
        </div>
      )}
    </UpdDialog>
  );
}
