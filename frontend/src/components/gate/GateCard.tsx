import type { ReactNode, Ref } from 'react';
import { BrandMark } from '../Layout';
import './gate.css';

export type GateIconKind = 'logo' | 'lock' | 'stopped' | 'windows';

/**
 * The card the app-window screens share (design D4): centered in the window, 20px radius,
 * 32px padding, an icon or the logo on top, then a 26px title. `width` is the card's max width.
 */
export function GateCard({
  width = 440,
  icon,
  title,
  titleId,
  headingRef,
  children,
  className,
}: {
  width?: 440 | 520 | 540;
  icon: GateIconKind;
  title: ReactNode;
  titleId: string;
  headingRef?: Ref<HTMLHeadingElement>;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className="gate">
      <main className={`gate-card gate-w-${width}${className ? ` ${className}` : ''}`} aria-labelledby={titleId}>
        <GateIcon kind={icon} />
        <h1 id={titleId} ref={headingRef} tabIndex={headingRef ? -1 : undefined} className="gate-title">
          {title}
        </h1>
        {children}
      </main>
    </div>
  );
}

export function GateIcon({ kind }: { kind: GateIconKind }) {
  if (kind === 'logo') {
    return (
      <span className="gate-logo">
        <BrandMark />
      </span>
    );
  }
  return (
    <span className={`gate-icon ${kind === 'stopped' ? 'is-neutral' : 'is-info'}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {kind === 'lock' && (
          <>
            <rect x="5" y="11" width="14" height="9" rx="2" />
            <path d="M8 11V8a4 4 0 0 1 8 0v3" />
          </>
        )}
        {kind === 'stopped' && (
          <>
            <circle cx="12" cy="12" r="9" />
            <path d="M9 9l6 6M15 9l-6 6" />
          </>
        )}
        {kind === 'windows' && (
          <>
            <rect x="3" y="7" width="13" height="11" rx="2" />
            <path d="M8 7V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3" />
          </>
        )}
      </svg>
    </span>
  );
}
