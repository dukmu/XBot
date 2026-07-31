import { FileText, Paperclip, Square, Send, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ImageInput } from "../api/types";

export interface PendingAttachment extends ImageInput {
  name: string;
  preview?: string;
}

interface ComposerProps {
  running: boolean;
  queued: number;
  onSend: (content: string, attachments: PendingAttachment[]) => Promise<void>;
  onInterrupt: () => Promise<void>;
}

export function Composer({ running, queued, onSend, onInterrupt }: ComposerProps) {
  const [content, setContent] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(180, Math.max(46, element.scrollHeight))}px`;
  }, [content]);

  const submit = () => {
    const value = content.trim();
    if (!value && attachments.length === 0) return;
    setContent("");
    const submitted = attachments;
    setAttachments([]);
    setAttachmentError("");
    void onSend(value, submitted);
  };

  const addFiles = async (files: FileList | File[]) => {
    setAttachmentError("");
    const accepted: PendingAttachment[] = [];
    for (const file of Array.from(files)) {
      try {
        const encoded = await readDataUrl(file);
        accepted.push({
          name: file.name,
          media_type: file.type || "application/octet-stream",
          data: encoded.slice(encoded.indexOf(",") + 1),
          preview: file.type.startsWith("image/") ? encoded : undefined,
        });
      } catch {
        setAttachmentError(`Unable to read attachment: ${file.name}`);
      }
    }
    if (accepted.length) {
      setAttachments((current) => [...current, ...accepted]);
    }
  };

  return (
    <div className="composer-wrap">
      {queued > 0 && <div className="queue-indicator">{queued} queued</div>}
      <div className="composer">
        {attachments.length > 0 && (
          <div className="composer-images">
            {attachments.map((attachment, index) => (
              <div className="composer-image" key={`${attachment.name}-${index}`} title={attachment.name}>
                {attachment.preview ? <img src={attachment.preview} alt={attachment.name} /> : <FileText size={24} />}
                <button title={`Remove ${attachment.name}`} aria-label={`Remove ${attachment.name}`} onClick={() => setAttachments((current) => current.filter((_, item) => item !== index))}>
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
        {attachmentError && <div className="attachment-error">{attachmentError}</div>}
        <textarea
          ref={textarea}
          value={content}
          rows={1}
          placeholder="Message XBot"
          aria-label="Message XBot"
          onChange={(event) => setContent(event.target.value)}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.files);
            if (files.length) {
              event.preventDefault();
              void addFiles(files);
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <div className="composer-footer">
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            onChange={(event) => {
              if (event.target.files) void addFiles(event.target.files);
              event.target.value = "";
            }}
          />
          <button className="composer-tool" title="Attach images" aria-label="Attach images" onClick={() => fileInput.current?.click()}>
            <Paperclip size={15} />
          </button>
          <span className="composer-spacer" />
          {running ? (
            <button className="composer-action stop" title="Interrupt" aria-label="Interrupt" onClick={() => void onInterrupt()}>
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button className="composer-action" title="Send" aria-label="Send" disabled={!content.trim() && attachments.length === 0} onClick={submit}>
              <Send size={15} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
