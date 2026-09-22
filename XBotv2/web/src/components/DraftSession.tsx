import { FolderSearch, Menu, TerminalSquare } from "lucide-react";
import type { ReactNode } from "react";
import { Composer } from "./Composer";
import type { CommandInfo, UsageData } from "../api/types";

/**
 * Cold start, ported from the DeepSeek Harness Web UI: booting shows a session
 * rather than a "nothing selected" page, so the composer is already on screen
 * and locked, and choosing a workspace is what unlocks it.
 */
export function DraftSession({
  commands,
  usage,
  inputHistory,
  accessMode,
  runtimeControls,
  onOpenCommands,
  onChooseWorkspace,
  onOpenSessions,
}: {
  commands: CommandInfo[];
  usage: UsageData;
  inputHistory: string[];
  accessMode?: ReactNode;
  runtimeControls?: ReactNode;
  onOpenCommands?: () => void;
  onChooseWorkspace: () => void;
  onOpenSessions: () => void;
}) {
  return (
    <div className="conversation-scroll draft-session" data-conversation-scroll>
      <div className="draft-session-body">
        <div className="draft-session-hero">
          <TerminalSquare size={42} strokeWidth={1.5} />
          <h1>XBot</h1>
          <p>Choose a workspace to start a session</p>
          <button
            className="primary-button draft-session-choose"
            data-testid="choose-workspace"
            onClick={onChooseWorkspace}
          >
            <FolderSearch size={16} /> Choose workspace
          </button>
          <button className="mobile-session-button" onClick={onOpenSessions}>
            <Menu size={16} /> Sessions
          </button>
        </div>
        <Composer
          running={false}
          disabled
          commands={commands}
          draft={null}
          allowImages={false}
          usage={usage}
          contextWindow={0}
          onSend={async () => false}
          inputHistory={inputHistory}
          onSubmitted={() => undefined}
          onInterrupt={async () => undefined}
          onOpenCommands={onOpenCommands}
          accessMode={accessMode}
          runtimeControls={runtimeControls}
        />
      </div>
    </div>
  );
}
