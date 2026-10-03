/**
 * The Iron Owl logo, drawn inline (no image request, no flash): the same drawing as the app icon
 * (packaging/brand/iron-owl.svg = public/favicon.svg). Fixed colors, so it reads the same in every
 * theme. The boot screen in index.html carries a plain-markup copy; keep the two in step.
 */
export function OwlLogo({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 512 512" aria-hidden="true" focusable="false">
      <rect width="512" height="512" rx="116" fill="#F2A30F" />
      <path fill="#2D3540" d="M158 108 L208 150 Q256 140 304 150 L354 108 Q368 100 370 118 L374 210 Q440 290 414 378 Q382 456 256 458 Q130 456 98 378 Q72 290 138 210 L142 118 Q144 100 158 108 Z" />
      <circle cx="196" cy="242" r="64" fill="#F6F8FA" />
      <circle cx="316" cy="242" r="64" fill="#F6F8FA" />
      <circle cx="202" cy="248" r="32" fill="#2D3540" />
      <circle cx="310" cy="248" r="32" fill="#2D3540" />
      <circle cx="214" cy="236" r="11" fill="#FFFFFF" />
      <circle cx="322" cy="236" r="11" fill="#FFFFFF" />
      <path fill="#F2A30F" d="M242 296 Q256 290 270 296 L259 318 Q256 323 253 318 Z" />
      <ellipse cx="256" cy="388" rx="74" ry="56" fill="#DDE3EA" />
      <circle cx="256" cy="378" r="17" fill="#2D3540" />
      <path fill="#2D3540" d="M248 386 L264 386 L270 416 L242 416 Z" />
      <ellipse cx="218" cy="458" rx="22" ry="12" fill="#2D3540" />
      <ellipse cx="294" cy="458" rx="22" ry="12" fill="#2D3540" />
    </svg>
  );
}
