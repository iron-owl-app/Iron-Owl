import { RejectedFileDialog } from './RejectedFileDialog';
import { WhatsNewDialog } from './WhatsNewDialog';

/** The updates pop-ups, inside the open app (Layout): What's new and a picked file's answer. */
export function UpdateDialogs() {
  return (
    <>
      <WhatsNewDialog />
      <RejectedFileDialog />
    </>
  );
}
