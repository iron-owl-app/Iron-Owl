import { Fragment } from 'react';

const CODE_RE = /(FT-[A-Z0-9]+(?:-[A-Z0-9]+)*)/g;

/**
 * A Details line with its short codes ("FT-UPD-03", "FT-UPD-HELP-SPACE") kept on one line:
 * browsers may break a line after a hyphen, and a code split in two is hard to read out.
 */
export function CodeText({ text }: { text: string }) {
  const parts = text.split(CODE_RE);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <span key={i} className="ft-code" style={{ whiteSpace: 'nowrap' }}>
            {part}
          </span>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  );
}
