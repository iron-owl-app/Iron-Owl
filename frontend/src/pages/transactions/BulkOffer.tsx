import { useEffect, useId, useState } from 'react';
import { api, errorMessage, type RecategorizePreview } from '../../api';
import { plural } from '../../lib/format';
import { Icon } from '../../components/Icon';
import { useToast } from '../../components/Toast';

export type RuleOffer = { txnId: number; field: 'merchant' | 'name'; text: string; category: string; categoryName: string };

/**
 * After a category change on the selected transaction: change the other transactions from
 * the same merchant too (one time). "Always" (a rule) lives in the Undo toast instead.
 * Stays hidden when there's nothing else to change.
 */
export function BulkOffer({ offer, onClose, onApplied }: { offer: RuleOffer; onClose: () => void; onApplied: () => void }) {
  const uid = useId();
  const toast = useToast();
  const [preview, setPreview] = useState<RecategorizePreview | null>(null);
  const [includeManual, setIncludeManual] = useState(false);
  const [busy, setBusy] = useState(false);
  const { field, text, category, categoryName } = offer;

  useEffect(() => {
    let alive = true;
    setPreview(null);
    setIncludeManual(false);
    api.recategorize
      .preview({ field, text, category })
      .then((p) => alive && setPreview(p))
      .catch(() => {
        /* no offer without a count */
      });
    return () => {
      alive = false;
    };
  }, [field, text, category]);

  if (!preview || preview.matches === 0) return null;
  const willChange = preview.matches - (includeManual ? 0 : preview.manual);

  async function apply() {
    setBusy(true);
    try {
      const res = await api.recategorize.apply({ field, text, category, include_manual: includeManual });
      const { changed } = res;
      // "Paying off debt" (loans only): the server leaves card payments out and says how many.
      const cardsLeft = (res as { skipped?: number }).skipped ?? 0;
      const skipped = preview && !includeManual ? preview.manual : 0;
      toast.push({
        tone: 'success',
        title: changed ? `Changed ${plural(changed, `${text} transaction`)}` : 'Nothing else to change',
        body: `They’re now ${categoryName}.${skipped ? ` Left ${skipped.toLocaleString()} you set by hand as ${skipped === 1 ? 'it was' : 'they were'}.` : ''}${
          cardsLeft ? ` ${plural(cardsLeft, 'card payment')} ${cardsLeft === 1 ? 'was' : 'were'} left out: ${cardsLeft === 1 ? 'it’s' : 'they’re'} already part of your Budget money.` : ''
        }`,
      });
      onApplied();
    } catch (e) {
      toast.push({ tone: 'error', title: 'Couldn’t change the other transactions', body: errorMessage(e) });
      setBusy(false);
    }
  }

  return (
    <div className="txn-offer" role="group" aria-labelledby={`${uid}-q`}>
      <Icon name="rule" />
      <div className="txn-offer-main">
        <p id={`${uid}-q`}>
          Change the other {plural(preview.matches, `${text} transaction`)} to <strong>{categoryName}</strong> too?
        </p>
        {preview.manual > 0 && (
          <label className="check bulk-manual">
            <input type="checkbox" checked={includeManual} onChange={(e) => setIncludeManual(e.target.checked)} disabled={busy} />
            <span>Also change {preview.manual.toLocaleString()} you set by hand</span>
          </label>
        )}
        <div className="txn-offer-actions">
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void apply()} disabled={busy || willChange === 0}>
            {busy ? 'Changing…' : `Change ${plural(willChange, 'transaction')}`}
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose} disabled={busy}>
            Just this one
          </button>
        </div>
      </div>
    </div>
  );
}
