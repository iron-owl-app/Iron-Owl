import { useMemo, type ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import helpText from '../../../HELP.md?raw';
import { useApp } from '../state';
import { Icon } from '../components/Icon';
import { parseHelp, type Block, type Inline } from '../lib/helpMarkdown';
import './help.css';

/**
 * Help (`#/help`): HELP.md from the repo root, bundled into the app so it works offline and while
 * Iron Owl is locked. A "who to call" card sits on top when a support contact is set.
 *
 * `HelpScreen` is the same page shown by the Gate outside the app frame (locked, setup, already
 * open elsewhere), with a Back button instead of the menu.
 */
export function HelpPage() {
  return <HelpContent standalone={false} />;
}

export function HelpScreen() {
  return <HelpContent standalone />;
}

function HelpContent({ standalone }: { standalone: boolean }) {
  const { authInfo } = useApp();
  const blocks = useMemo(() => parseHelp(helpText), []);
  const page = (
    <article className="help-page" aria-labelledby="help-title">
      {standalone && <BackButton />}
      <ContactCard contact={authInfo.supportContact} />
      <div className="help-doc">
        {blocks.map((b, i) => (
          <HelpBlock key={i} block={b} first={i === 0} />
        ))}
      </div>
      {standalone && <BackButton bottom />}
    </article>
  );
  return standalone ? <main className="help-standalone">{page}</main> : page;
}

/** "Need help? Ask Sam 555-0100." Hidden when no one is named. */
function ContactCard({ contact }: { contact: string | null }) {
  if (!contact) return null;
  return (
    <section className="help-contact" aria-label="Who to call">
      <Icon name="help" />
      <p>
        <strong>Need help?</strong> Ask {contact.replace(/[.!]+$/, '')}.
      </p>
    </section>
  );
}

/** Back to wherever Help was opened from (the password screen), or the start if opened directly. */
function BackButton({ bottom = false }: { bottom?: boolean }) {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <button
      type="button"
      className={`btn btn-lg help-back${bottom ? ' is-bottom' : ''}`}
      onClick={() => (location.key !== 'default' ? navigate(-1) : navigate('/', { replace: true }))}
    >
      <span aria-hidden="true">←</span>
      Back
    </button>
  );
}

function HelpBlock({ block, first }: { block: Block; first: boolean }) {
  switch (block.kind) {
    case 'heading': {
      const kids = <InlineNodes nodes={block.children} />;
      // The document's first heading is the page title (h1); the rest step down from there.
      if (block.level === 1)
        return (
          <h1 id={first ? 'help-title' : block.id} className="help-h1">
            {kids}
          </h1>
        );
      if (block.level === 2)
        return (
          <h2 id={block.id} className="help-h2">
            {kids}
          </h2>
        );
      return (
        <h3 id={block.id} className="help-h3">
          {kids}
        </h3>
      );
    }
    case 'paragraph':
      return (
        <p className="help-p">
          <InlineNodes nodes={block.children} />
        </p>
      );
    case 'list': {
      const items = block.items.map((it, i) => (
        <li key={i}>
          <InlineNodes nodes={it} />
        </li>
      ));
      return block.ordered ? (
        <ol className="help-list help-steps" start={block.start}>
          {items}
        </ol>
      ) : (
        <ul className="help-list">{items}</ul>
      );
    }
  }
}

/** Text is rendered as React text (never as HTML), so tags in HELP.md show as typed. */
function InlineNodes({ nodes }: { nodes: Inline[] }): ReactNode {
  return nodes.map((n, i) => {
    if (n.kind === 'text') return n.text;
    if (n.kind === 'bold')
      return (
        <strong key={i}>
          <InlineNodes nodes={n.children} />
        </strong>
      );
    if (n.external)
      return (
        <a key={i} className="help-link" href={n.href} target="_blank" rel="noopener noreferrer">
          <InlineNodes nodes={n.children} />
          <Icon name="external" className="help-ext" />
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      );
    return (
      <Link key={i} className="help-link" to={n.href.slice(1)}>
        <InlineNodes nodes={n.children} />
      </Link>
    );
  });
}
