import { Square, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";

interface ComposerProps {
  running: boolean;
  queued: number;
  onSend: (content: string) => Promise<void>;
  onInterrupt: () => Promise<void>;
}

export function Composer({ running, queued, onSend, onInterrupt }: ComposerProps) {
  const [content, setContent] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(180, Math.max(46, element.scrollHeight))}px`;
  }, [content]);

  const submit = () => {
    const value = content.trim();
    if (!value) return;
    setContent("");
    void onSend(value);
  };

  return (
    <div className="composer-wrap">
      {queued > 0 && <div className="queue-indicator">{queued} queued</div>}
      <div className="composer">
        <textarea
          ref={textarea}
          value={content}
          rows={1}
          placeholder="Message XBot"
          aria-label="Message XBot"
          onChange={(event) => setContent(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-footer">
          {running ? (
            <button className="composer-action stop" title="Interrupt" aria-label="Interrupt" onClick={() => void onInterrupt()}>
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button className="composer-action" title="Send" aria-label="Send" disabled={!content.trim()} onClick={submit}>
              <Send size={15} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
