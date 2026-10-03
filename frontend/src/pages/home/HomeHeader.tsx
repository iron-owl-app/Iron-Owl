import { useApp, useUpdated } from '../../state';
import { useToast } from '../../components/Toast';
import { greeting, headerDate } from './homeFormat';

/**
 * "Good morning", today's date and "· Updated 2 hours ago" (the shared time the sidebar and
 * Accounts show, state.tsx `useUpdated`), with "Update from banks" when there's a bank to update
 * from. A good update says "Updated from your banks just now."; a bank that failed is named, and
 * "Things that need you" says what to do.
 */
export function HomeHeader({ now, showUpdate }: { now: Date; showUpdate: boolean }) {
  const { sync, syncing } = useApp();
  const shared = useUpdated();
  const toast = useToast();

  async function update() {
    if (syncing) return;
    const results = await sync({ quiet: 'all' });
    if (!results) return; // already running, or it failed (state.tsx says so)
    if (results.length === 0) {
      toast.push({ tone: 'info', title: 'There’s no bank to update from yet.' });
      return;
    }
    const failed = results.filter((r) => !r.ok);
    if (failed.length === 0) {
      toast.push({ tone: 'success', title: 'Updated from your banks just now.' });
      return;
    }
    const title =
      failed.length === 1 ? `${failed[0]!.institution_name ?? 'One bank'} couldn’t be updated` : `${failed.length} banks couldn’t be updated`;
    const body =
      failed.length === results.length
        ? 'See “Things that need you” at the top of this page.'
        : 'The others are up to date. See “Things that need you” at the top of this page.';
    toast.push({ tone: 'warn', title, body });
  }

  const updated = !showUpdate ? null : syncing ? 'Updating from your banks…' : shared.text;

  return (
    <header className="home-head">
      <div>
        <h1 className="home-hello">{greeting(now)}</h1>
        <p className="home-date">
          {headerDate(now)}
          {updated && ` · ${updated}`}
        </p>
      </div>
      {showUpdate && (
        <button type="button" className="home-btn home-btn-outline" onClick={() => void update()} aria-disabled={syncing || undefined}>
          {syncing ? 'Updating…' : 'Update from banks'}
        </button>
      )}
    </header>
  );
}
