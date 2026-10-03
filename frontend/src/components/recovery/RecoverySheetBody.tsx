import { useId, useState } from 'react';
import type { RecoverySheetData } from '../../api';
import { Icon } from '../Icon';
import { RecoveryCodeGrid } from './RecoveryCodeGrid';
import { RecoverySheetPrint } from './RecoverySheetPrint';
import { sheetDate } from './recoveryFormat';
import './recovery.css';

const TIPS: [string, string][] = [
  ['Keep it with your papers', 'Where you keep your passport or insurance papers.'],
  ['Not near the computer', 'Anyone with the sheet and the computer can get in.'],
  ['No photos or email', 'Paper only. Don’t take a picture of it.'],
];

/**
 * A new sheet's numbers with Print, "I wrote it down", three tips and the "I have printed this
 * sheet" checkbox. Shared by the setup step and Settings' "Your new recovery sheet" dialog;
 * the caller renders Continue / Done, gated on `checked`.
 */
export function RecoverySheetBody({
  data,
  checked,
  onChecked,
  compact,
}: {
  data: RecoverySheetData;
  checked: boolean;
  onChecked: (v: boolean) => void;
  /** Dialog layout: slightly smaller digits and buttons. */
  compact?: boolean;
}) {
  const checkId = useId();
  const [wrote, setWrote] = useState(false);
  const made = sheetDate(data.created_at);

  return (
    <div className={`rs-body${compact ? ' rs-body-compact' : ''}`}>
      <div className="rs-card">
        <RecoveryCodeGrid groups={data.groups} compact={compact} />
        <div className="rs-sheetline">
          Sheet {data.sheet}
          {made ? ` · made ${made}` : ''}
        </div>
      </div>

      <div className="rs-actions">
        <button type="button" className={`btn rs-btn ${compact ? 'rs-btn-outline' : 'btn-primary rs-btn-main'}`} onClick={() => window.print()}>
          <Icon name="printer" />
          Print recovery sheet
        </button>
        <button
          type="button"
          className="btn rs-btn rs-btn-outline"
          onClick={() => {
            onChecked(true);
            setWrote(true);
          }}
        >
          I wrote it down
        </button>
      </div>
      <p className="rs-hint">
        If your computer asks where to <strong>save a file</strong>, it isn’t printing. Pick your printer instead.
      </p>
      <div role="status" className="rs-live">
        {wrote && <p className="rs-good">Good. Keep the paper with your important papers.</p>}
      </div>

      <ul className="rs-tips">
        {TIPS.map(([title, body]) => (
          <li key={title}>
            <strong>{title}</strong>
            {body}
          </li>
        ))}
      </ul>

      <label className={`rs-check${checked ? ' is-checked' : ''}`} htmlFor={checkId}>
        <input id={checkId} type="checkbox" checked={checked} onChange={(e) => onChecked(e.target.checked)} />
        <span>I have printed this sheet or written the numbers down.</span>
      </label>

      <RecoverySheetPrint data={data} />
    </div>
  );
}
