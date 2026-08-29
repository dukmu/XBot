import { FileText, Paperclip, Square, Send, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { CommandInfo, ImageInput } from "../api/types";
import { matchingCommands } from "../commands";

export interface PendingAttachment extends ImageInput {
  name: string;
  preview?: string;
}

interface ComposerProps {
  running: boolean;
  disabled: boolean;
  queued: number;
  commands: CommandInfo[];
  draft: { id: number; value: string } | null;
  allowImages: boolean;
  onSend: (content: string, attachments: PendingAttachment[]) => Promise<boolean>;
  onInterrupt: () => Promise<void>;
}

export function Composer({ running, disabled, queued, commands, draft, allowImages, onSend, onInterrupt }: ComposerProps) {
  const [content, setContent] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [commandMenuOpen, setCommandMenuOpen] = useState(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const suggestions = commandMenuOpen ? matchingCommands(commands, content).slice(0, 9) : [];

  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(180, Math.max(46, element.scrollHeight))}px`;
  }, [content]);

  useEffect(() => setCommandIndex(0), [content]);

  useEffect(() => {
    if (!draft) return;
    setContent(draft.value);
    setCommandMenuOpen(false);
    requestAnimationFrame(() => textarea.current?.focus());
  }, [draft]);

  const submit = async () => {
    const value = content.trim();
    if (!value && attachments.length === 0) return;
    setContent("");
    setCommandMenuOpen(false);
    const submitted = attachments;
    setAttachments([]);
    setAttachmentError("");
    if (!await onSend(value, submitted)) {
      setContent((current) => current || value);
      setAttachments((current) => [...submitted, ...current]);
    }
  };

  const completeCommand = (command: CommandInfo) => {
    setContent(`${command.slash}${command.usage === command.slash ? "" : " "}`);
    setCommandMenuOpen(false);
    requestAnimationFrame(() => textarea.current?.focus());
  };

  const addFiles = async (files: FileList | File[]) => {
    setAttachmentError("");
    const accepted: PendingAttachment[] = [];
    for (const file of Array.from(files)) {
      if (file.type.startsWith("image/") && !allowImages) {
        setAttachmentError("The selected model does not accept image input.");
        continue;
      }
      try {
        const encoded = await readDataUrl(file);
        accepted.push({
          name: file.name || pastedFileName(file.type, accepted.length),
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
        {suggestions.length > 0 && (
          <div className="command-menu" role="listbox" aria-label="Commands">
            {suggestions.map((command, index) => (
              <button
                type="button"
                role="option"
                aria-selected={index === commandIndex}
                className={index === commandIndex ? "selected" : ""}
                key={`${command.kind}:${command.name}`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => completeCommand(command)}
              >
                <span><b>{command.slash}</b><small>{command.kind}</small></span>
                <span>{command.description}</span>
                <code>{command.usage}</code>
              </button>
            ))}
          </div>
        )}
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
          disabled={disabled}
          rows={1}
          placeholder="Message XBot · paste images"
          aria-label="Message XBot"
          onChange={(event) => {
            setContent(event.target.value);
            setCommandMenuOpen(true);
          }}
          onPaste={(event) => {
            const files = clipboardFiles(event.clipboardData);
            if (files.length) {
              event.preventDefault();
              void addFiles(files);
            }
          }}
          onKeyDown={(event) => {
            if (suggestions.length && event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setCommandMenuOpen(false);
              return;
            }
            if (suggestions.length && event.key === "ArrowDown") {
              event.preventDefault();
              setCommandIndex((current) => (current + 1) % suggestions.length);
              return;
            }
            if (suggestions.length && event.key === "ArrowUp") {
              event.preventDefault();
              setCommandIndex((current) => (current - 1 + suggestions.length) % suggestions.length);
              return;
            }
            if (suggestions.length && event.key === "Tab") {
              event.preventDefault();
              completeCommand(suggestions[commandIndex]);
              return;
            }
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              void submit();
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
          <button className="composer-tool" title="Attach files" aria-label="Attach files" disabled={disabled} onClick={() => fileInput.current?.click()}>
            <Paperclip size={15} />
          </button>
          <span className="composer-spacer" />
          {running ? (
            <button className="composer-action stop" title="Interrupt" aria-label="Interrupt" onClick={() => void onInterrupt()}>
              <Square size={14} fill="currentColor" />
            </button>
          ) : (
            <button className="composer-action" title="Send" aria-label="Send" disabled={disabled || (!content.trim() && attachments.length === 0)} onClick={() => void submit()}>
              <Send size={15} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function clipboardFiles(clipboard: DataTransfer): File[] {
  const itemFiles = Array.from(clipboard.items)
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null);
  return itemFiles.length ? itemFiles : Array.from(clipboard.files);
}

function pastedFileName(mediaType: string, index: number): string {
  const subtype = mediaType.split("/", 2)[1]?.replace(/[^A-Za-z0-9.+-]/g, "") || "bin";
  return `pasted-${Date.now()}-${index + 1}.${subtype}`;
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
