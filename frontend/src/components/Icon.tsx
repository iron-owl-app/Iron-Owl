import type { ReactNode, SVGProps } from 'react';
import type { Category } from '../api';

/** Authored stroke icons: 24px grid, 1.75 stroke, round joins. */
const PATHS = {
  dashboard: (
    <>
      <rect x="3.5" y="3.5" width="7" height="8.5" rx="1.75" />
      <rect x="13.5" y="3.5" width="7" height="5" rx="1.75" />
      <rect x="13.5" y="11.5" width="7" height="9" rx="1.75" />
      <rect x="3.5" y="15" width="7" height="5.5" rx="1.75" />
    </>
  ),
  wallet: (
    <>
      <path d="M5 7.5V6.2A2.2 2.2 0 0 1 7.2 4h9.3" />
      <rect x="3.5" y="7.5" width="17" height="12.5" rx="2.5" />
      <path d="M16 13.75h1.5" />
    </>
  ),
  arrows: (
    <>
      <path d="M7.5 4 4 7.5 7.5 11" />
      <path d="M4 7.5h14" />
      <path d="m16.5 13 3.5 3.5-3.5 3.5" />
      <path d="M20 16.5H6" />
    </>
  ),
  trend: (
    <>
      <path d="m3.5 16.5 5.5-5.5 4 4 7.5-7.5" />
      <path d="M15 7.5h5.5V13" />
    </>
  ),
  percent: (
    <>
      <path d="M18.5 5.5 5.5 18.5" />
      <circle cx="7.5" cy="7.5" r="2.5" />
      <circle cx="16.5" cy="16.5" r="2.5" />
    </>
  ),
  sliders: (
    <>
      <path d="M4 7h9M18 7h2M4 17h3M11 17h9" />
      <circle cx="15.5" cy="7" r="2.25" />
      <circle cx="9" cy="17" r="2.25" />
    </>
  ),
  sync: (
    <>
      <path d="M19.5 11A7.5 7.5 0 0 0 6.2 6.8L4.5 8.5" />
      <path d="M4.5 4.5v4h4" />
      <path d="M4.5 13a7.5 7.5 0 0 0 13.3 4.2l1.7-1.7" />
      <path d="M19.5 19.5v-4h-4" />
    </>
  ),
  lock: (
    <>
      <rect x="4.5" y="10.5" width="15" height="10" rx="2.5" />
      <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
      <path d="M12 14.5v2" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  eye: (
    <>
      <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  eyeOff: (
    <>
      <path d="m3.5 3.5 17 17" />
      <path d="M10.6 5.6c.46-.07.93-.1 1.4-.1 6 0 9.5 6.5 9.5 6.5a16.6 16.6 0 0 1-2.7 3.5M6.6 7.2A16.3 16.3 0 0 0 2.5 12S6 18.5 12 18.5c1.5 0 2.9-.4 4.1-1" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    </>
  ),
  chart: (
    <>
      <path d="M3.5 3.5v17h17" />
      <path d="m7.5 14.5 3.5-4 3 3 5-6" />
    </>
  ),
  pencil: (
    <>
      <path d="M4 20h4L19 9a2.83 2.83 0 0 0-4-4L4 16v4Z" />
      <path d="m13.5 6.5 4 4" />
    </>
  ),
  trash: (
    <>
      <path d="M4 7h16" />
      <path d="M10 11v6M14 11v6" />
      <path d="m6 7 1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" />
      <path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6 6 18" />,
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  alert: (
    <>
      <path d="M10.3 4.3 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9.5v4" />
      <path d="M12 17h.01" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5" />
      <path d="M12 8h.01" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.7" />
      <path d="M12 17h.01" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4 4 0 0 0 5.66 0l3.1-3.1a4 4 0 0 0-5.66-5.66L12 6.34" />
      <path d="M14 10a4 4 0 0 0-5.66 0l-3.1 3.1a4 4 0 0 0 5.66 5.66L12 17.66" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-4.4-4.4" />
    </>
  ),
  chevronLeft: <path d="m14.5 6-6 6 6 6" />,
  chevronRight: <path d="m9.5 6 6 6-6 6" />,
  undo: (
    <>
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
    </>
  ),
  chevronDown: <path d="m6 9.5 6 6 6-6" />,
  up: (
    <>
      <path d="M7 17 17 7" />
      <path d="M8.5 7H17v8.5" />
    </>
  ),
  down: (
    <>
      <path d="M7 7l10 10" />
      <path d="M17 8.5V17H8.5" />
    </>
  ),
  flat: <path d="M5 12h14" />,
  bank: (
    <>
      <path d="M3.5 9.5 12 4.5l8.5 5" />
      <path d="M4.5 20h15" />
      <path d="M6 10.5v6.5M10 10.5v6.5M14 10.5v6.5M18 10.5v6.5" />
    </>
  ),
  heart: (
    <>
      <path d="M12 20s-7.5-4.4-7.5-10A4.5 4.5 0 0 1 12 7.3 4.5 4.5 0 0 1 19.5 10c0 5.6-7.5 10-7.5 10Z" />
      <path d="M12 10.5v4M10 12.5h4" />
    </>
  ),
  hourglass: (
    <>
      <path d="M6.5 3.5h11M6.5 20.5h11" />
      <path d="M7.5 3.5c0 4.5 4.5 5 4.5 8.5s-4.5 4-4.5 8.5M16.5 3.5c0 4.5-4.5 5-4.5 8.5s4.5 4 4.5 8.5" />
    </>
  ),
  pie: (
    <>
      <path d="M11 4a8.5 8.5 0 1 0 9 9h-9Z" />
      <path d="M14 3.6A8.5 8.5 0 0 1 20.4 10H14Z" />
    </>
  ),
  house: (
    <>
      <path d="M4 10.5 12 4l8 6.5" />
      <path d="M6 9v11h12V9" />
      <path d="M10 20v-5h4v5" />
    </>
  ),
  card: (
    <>
      <rect x="3" y="5.5" width="18" height="13" rx="2.5" />
      <path d="M3 10h18" />
      <path d="M7 15h3" />
    </>
  ),
  box: (
    <>
      <path d="M3.5 7.5 12 3.5l8.5 4v9L12 20.5l-8.5-4Z" />
      <path d="M3.5 7.5 12 11.5l8.5-4M12 11.5v9" />
    </>
  ),
  calendar: (
    <>
      <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
      <path d="M3.5 10h17M8 3v4M16 3v4" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3.5 5 6.2v5.3c0 4.3 3 7.7 7 9 4-1.3 7-4.7 7-9V6.2L12 3.5Z" />
      <path d="m9 12 2 2 4-4" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 8.5-8.5M16 7l2.5 2.5" />
    </>
  ),
  logo: <path d="M5 16.5 10 11l3.5 3.5L19 8" />,
  // Release 2 nav icons (Sidebar design)
  receipt: (
    <>
      <path d="M6 3.5h12v17l-3-2-3 2-3-2-3 2Z" />
      <path d="M9 8.5h6M9 12.5h6" />
    </>
  ),
  basket: (
    <>
      <path d="m8 8 4-5 4 5" />
      <path d="M3 8h18l-2 11a2 2 0 0 1-2 1.5H7A2 2 0 0 1 5 19Z" />
      <path d="M9 12v4M15 12v4" />
    </>
  ),
  repeat: (
    <>
      <path d="m17 3.5 3 3-3 3" />
      <path d="M20 6.5H8a4 4 0 0 0-4 4v1" />
      <path d="m7 20.5-3-3 3-3" />
      <path d="M4 17.5h12a4 4 0 0 0 4-4v-1" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="4.5" />
      <path d="M12 12h.01" />
    </>
  ),
  bell: (
    <>
      <path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15Z" />
      <path d="M10 20.5a2 2 0 0 0 4 0" />
    </>
  ),
  // --- icons added by frontend agent A go below this line
  sparkle: (
    <>
      <path d="M12 4.5c.6 3.6 1.9 4.9 5.5 5.5-3.6.6-4.9 1.9-5.5 5.5-.6-3.6-1.9-4.9-5.5-5.5 3.6-.6 4.9-1.9 5.5-5.5Z" />
      <path d="M18.5 15.5c.25 1.5.75 2 2.25 2.25-1.5.25-2 .75-2.25 2.25-.25-1.5-.75-2-2.25-2.25 1.5-.25 2-.75 2.25-2.25Z" />
    </>
  ),
  table: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
      <path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10" />
    </>
  ),
  // --- icons added by frontend agent B go below this line
  split: (
    <>
      <path d="M12 20.5v-7" />
      <path d="M12 13.5 6 7.5M12 13.5l6-6" />
      <path d="M6 12V7.5h4.5M18 12V7.5h-4.5" />
    </>
  ),
  folder: <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" />,
  download: (
    <>
      <path d="M12 4v11" />
      <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
      <path d="M4.5 19.5h15" />
    </>
  ),
  upload: (
    <>
      <path d="M12 15.5v-11" />
      <path d="M7.5 9 12 4.5 16.5 9" />
      <path d="M4.5 19.5h15" />
    </>
  ),
  arrowUp: <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" />,
  arrowDown: <path d="M12 5v14M6.5 13.5 12 19l5.5-5.5" />,
  note: (
    <>
      <path d="M5.5 3.5h9l4 4v13h-13Z" />
      <path d="M14.5 3.5v4h4" />
      <path d="M8.5 12.5h7M8.5 16h5" />
    </>
  ),
  printer: (
    <>
      <path d="M7 9V3.5h10V9" />
      <rect x="3.5" y="9" width="17" height="8" rx="2" />
      <path d="M7 14h10v6.5H7Z" />
    </>
  ),
  tag: (
    <>
      <path d="M3.5 12.1V4.5a1 1 0 0 1 1-1h7.6a1 1 0 0 1 .7.3l8 8a1 1 0 0 1 0 1.4l-7.6 7.6a1 1 0 0 1-1.4 0l-8-8a1 1 0 0 1-.3-.7Z" />
      <path d="M8 8h.01" />
    </>
  ),
  enter: (
    <>
      <path d="M19.5 5v7a3 3 0 0 1-3 3h-11" />
      <path d="m9 11-3.5 4L9 19" />
    </>
  ),
  rule: (
    <>
      <path d="M4 6.5h10M4 12h7M4 17.5h10" />
      <path d="m16 10 2.5 2L16 14" />
    </>
  ),
  bolt: <path d="M13 3 5 13.5h6L10 21l8-10.5h-6Z" />,
  // Home "Today"
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  'cloud-alert': (
    <>
      <path d="M7 18a4.5 4.5 0 0 1-.6-9A6 6 0 0 1 18 9.5a4 4 0 0 1-.5 8.5Z" />
      <path d="M12 11v3.5M12 17h.01" />
    </>
  ),
  'trend-down': (
    <>
      <path d="m4 6 6 6 4-4 6 6" />
      <path d="M20 10v4h-4" />
    </>
  ),
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  /* Colors (theme switch): a circle, half filled. */
  theme: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 4a8 8 0 0 1 0 16Z" fill="currentColor" />
    </>
  ),
  'arrow-right': <path d="M5 12h14M13 6l6 6-6 6" />,
  external: (
    <>
      <path d="M14 4.5h5.5V10" />
      <path d="M19.5 4.5 11 13" />
      <path d="M18 14v4a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 5 18V8a1.5 1.5 0 0 1 1.5-1.5h4" />
    </>
  ),
} satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className, ...rest }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className={className ? `icon ${className}` : 'icon'} {...rest}>
      {PATHS[name]}
    </svg>
  );
}

export const CATEGORY_ICON: Record<Category, IconName> = {
  bank: 'bank',
  hsa: 'heart',
  retirement: 'hourglass',
  investment: 'pie',
  loan: 'house',
  credit: 'card',
  other: 'box',
};

export function CategoryChip({ category }: { category: Category }) {
  return (
    <span className={`cat-chip cat-${category}`} aria-hidden="true">
      <Icon name={CATEGORY_ICON[category]} />
    </span>
  );
}
