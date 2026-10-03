import { useEffect, useState } from 'react';
import { useUpdates } from './UpdateProvider';
import { BANNER, bannerAnnounce, bannerLead } from './copy';

/** Offers already read out (once per update, not on every page change). */
const announced = new Set<string>();

/**
 * Banner (design D5 state 1): the first thing in <main>, edge to edge, not sticky. Both
 * Install update and What's new open the What's new pop-up, where the install really starts
 * (so the user always reads the backup and restart note first). Not now hides it until tomorrow.
 */
export function UpdateBanner() {
  const { bannerVisible, status, openNews, notNow } = useUpdates();
  const offer = status?.offer ?? null;
  const source = status?.source;
  const [news, setNews] = useState('');

  // Say it once for screen readers: the live region is mounted empty, then filled.
  useEffect(() => {
    if (!bannerVisible || !offer || announced.has(offer.id)) return;
    const t = window.setTimeout(() => {
      announced.add(offer.id);
      setNews(bannerAnnounce(source, offer.version));
    }, 400);
    return () => window.clearTimeout(t);
  }, [bannerVisible, offer, source]);

  if (!bannerVisible || !offer) return null;
  return (
    <section className="upd-banner" aria-label={BANNER.region}>
      <span className="upd-banner-ic" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 4v11M7 10l5 5 5-5" />
          <path d="M5 20h14" />
        </svg>
      </span>
      <p className="upd-banner-text">
        <strong>{bannerLead(source)}</strong> <span className="upd-banner-sub">{BANNER.detail(offer.version, offer.minutes)}</span>
      </p>
      <div className="upd-banner-actions">
        <button type="button" className="upd-btn upd-btn-44 upd-banner-primary" onClick={openNews}>
          {BANNER.install}
        </button>
        <button type="button" className="upd-btn upd-btn-44 upd-banner-outline" onClick={openNews}>
          {BANNER.news}
        </button>
        <button type="button" className="upd-btn upd-btn-44 upd-banner-ghost" onClick={notNow}>
          {BANNER.notNow}
        </button>
      </div>
      <p className="sr-only" role="status">
        {news}
      </p>
    </section>
  );
}
