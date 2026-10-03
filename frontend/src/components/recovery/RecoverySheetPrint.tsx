import { createPortal } from 'react-dom';
import type { RecoverySheetData } from '../../api';
import { formatGroup } from '../../lib/recoveryCode';
import { sheetDate } from './recoveryFormat';

/**
 * The printed recovery sheet (design screen 2). Portalled straight into <body> and hidden on
 * screen; the print styles in recovery.css hide everything else, so window.print() prints only
 * this page: black on white, one page on Letter or A4. Mounted only while the sheet is shown.
 */
export function RecoverySheetPrint({ data }: { data: RecoverySheetData }) {
  const made = sheetDate(data.created_at, 'long');
  return createPortal(
    <div className="rs-print-root" aria-hidden="true">
      <div className="rsp-head">
        <div className="rsp-title">Iron Owl Recovery Sheet</div>
        <div className="rsp-private">KEEP PRIVATE</div>
      </div>
      <div className="rsp-meta">
        Sheet number <strong>{data.sheet}</strong>
        {made ? ` · Made on ${made}` : ''}
      </div>
      <div className="rsp-computer">
        This computer: <span className="rsp-line" />
      </div>
      <div className="rsp-h">If you forget your password</div>
      <ol className="rsp-steps">
        <li>Open Iron Owl.</li>
        <li>Click “Forgot your password?”</li>
        <li>Type the 6 groups of numbers below.</li>
        <li>Choose a new password.</li>
      </ol>
      <div className="rsp-grid">
        {data.groups.map((g, i) => (
          <div className="rsp-box" key={i}>
            <span className="rsp-n">{i + 1}.</span>
            <span className="rsp-digits">{formatGroup(g)}</span>
          </div>
        ))}
      </div>
      <div className="rsp-important">
        <div className="rsp-h">Important</div>
        <ul>
          <li>Keep this sheet with your important papers, not next to the computer.</li>
          <li>Anyone who has this sheet and your computer can open Iron Owl.</li>
          <li>Don’t photograph or email it.</li>
          <li>If you make a new sheet, this one stops working.</li>
        </ul>
      </div>
      <div className="rsp-foot">
        <span>Iron Owl · Recovery Sheet {data.sheet}</span>
        <span>Page 1 of 1</span>
      </div>
    </div>,
    document.body,
  );
}
