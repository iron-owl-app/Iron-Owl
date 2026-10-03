import type { ReactNode } from 'react';

export type IllustrationKind = 'accounts' | 'transactions' | 'investments' | 'loans' | 'search' | 'offline' | 'chart';

/** Small line illustrations drawn with theme tokens (see .illo in styles.css). */
function Illustration({ kind }: { kind: IllustrationKind }) {
  const common = { className: 'illo', viewBox: '0 0 160 112', 'aria-hidden': true, focusable: false } as const;
  switch (kind) {
    case 'accounts':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="58" ry="6" />
          <rect className="i-card" x="34" y="18" width="84" height="54" rx="9" strokeWidth="1.5" transform="rotate(-8 76 45)" />
          <rect className="i-card" x="42" y="30" width="84" height="54" rx="9" strokeWidth="1.5" />
          <path className="i-line" d="M42 44h84" strokeWidth="1.5" />
          <path className="i-line" d="M54 64h22M54 72h14" strokeWidth="3" />
          <circle className="i-accent" cx="118" cy="80" r="15" strokeWidth="1.5" />
          <path className="i-ink" d="M118 73v14M111 80h14" strokeWidth="2" />
        </svg>
      );
    case 'transactions':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="52" ry="6" />
          <path className="i-card" d="M48 10h64v84l-8-5-8 5-8-5-8 5-8-5-8 5-8-5-8 5Z" strokeWidth="1.5" strokeLinejoin="round" />
          <path className="i-line" d="M60 28h26M60 42h34M60 56h22M60 70h30" strokeWidth="3" />
          <path className="i-line" d="M98 28h4M100 42h2M98 56h4M98 70h4" strokeWidth="3" />
          <circle className="i-accent" cx="112" cy="26" r="12" strokeWidth="1.5" />
          <path className="i-ink" d="m107 26 4 4 6-7" strokeWidth="2" />
        </svg>
      );
    case 'investments':
    case 'chart':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="58" ry="6" />
          <rect className="i-card" x="28" y="14" width="104" height="78" rx="10" strokeWidth="1.5" />
          <path className="i-line" d="M40 78h80" strokeWidth="1.5" />
          <rect className="i-fill" x="46" y="58" width="12" height="20" rx="3" />
          <rect className="i-fill" x="66" y="48" width="12" height="30" rx="3" />
          <rect className="i-fill" x="86" y="52" width="12" height="26" rx="3" />
          <rect className="i-accent" x="106" y="34" width="12" height="44" rx="3" strokeWidth="1.5" />
          <path className="i-ink" d="m44 46 18-10 18 6 26-16" strokeWidth="2" />
        </svg>
      );
    case 'loans':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="52" ry="6" />
          <rect className="i-card" x="44" y="12" width="72" height="84" rx="9" strokeWidth="1.5" />
          <path className="i-line" d="M58 30h44M58 42h30M58 54h38" strokeWidth="3" />
          <circle className="i-accent" cx="80" cy="76" r="12" strokeWidth="1.5" />
          <path className="i-ink" d="m75 81 10-10" strokeWidth="2" />
          <circle className="i-ink" cx="75.5" cy="71.5" r="1.6" strokeWidth="1.6" />
          <circle className="i-ink" cx="84.5" cy="80.5" r="1.6" strokeWidth="1.6" />
        </svg>
      );
    case 'search':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="46" ry="6" />
          <rect className="i-card" x="36" y="20" width="76" height="60" rx="9" strokeWidth="1.5" />
          <path className="i-line" d="M48 36h40M48 48h28M48 60h34" strokeWidth="3" />
          <circle className="i-accent" cx="106" cy="70" r="16" strokeWidth="1.5" />
          <path className="i-ink" d="m118 82 12 12" strokeWidth="3" />
        </svg>
      );
    case 'offline':
      return (
        <svg {...common}>
          <ellipse className="i-fill" cx="80" cy="100" rx="46" ry="6" />
          <rect className="i-card" x="40" y="22" width="80" height="58" rx="9" strokeWidth="1.5" />
          <path className="i-line" d="M52 68h56" strokeWidth="1.5" />
          <circle className="i-accent" cx="80" cy="46" r="13" strokeWidth="1.5" />
          <path className="i-ink" d="M80 40v7M80 52h.01" strokeWidth="2.5" />
        </svg>
      );
  }
}

export function EmptyState({
  kind,
  title,
  children,
  actions,
  compact,
}: {
  kind: IllustrationKind;
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`empty${compact ? ' empty-compact' : ''}`}>
      <Illustration kind={kind} />
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {actions && <div className="empty-actions">{actions}</div>}
    </div>
  );
}
