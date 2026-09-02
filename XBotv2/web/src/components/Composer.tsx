import { FileText, Paperclip, Square, Send, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { CommandInfo, ImageInput, UsageData } from "../api/types";
import { ContextMeter } from "./ContextMeter";
import { CommandTriggerMenu } from "./CommandTriggerMenu";
import { DropOverlay } from "./DropOverlay";
import { ImageLightbox } from "./ImageLightbox";
import { commandSuggestions } from "../commands";

export interface PendingAttachment extends ImageInput {
  name: string;
  preview?: string;
}

interface ComposerProps {
  running: boolean;
  disabled: boolean;
  commands: CommandInfo[];
  draft: { id: number; value: string } | null;
  allowImages: boolean;
  usage: UsageData;
  contextWindow: number;
  onSend: (content: string, attachments: PendingAttachment[]) => Promise<boolean>;
  onInterrupt: () => Promise<void>;
}

export function Composer({ running, disabled, commands, draft, allowImages, usage, contextWindow, onSend, onInterrupt }: ComposerProps) {
  const [content, setContent] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [commandMenuOpen, setCommandMenuOpen] = useState(true);
  const [caret, setCaret] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [attachmentPreview, setAttachmentPreview] = useState<PendingAttachment | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const closeAttachmentPreview = useCallback(() => setAttachmentPreview(null), []);
  const commandState = commandMenuOpen ? commandSuggestions(commands, content, caret) : null;
  const suggestions = commandState?.commands.slice(0, 9) ?? [];

  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(336, Math.max(46, element.scrollHeight))}px`;
  }, [content]);

  useEffect(() => setCommandIndex(0), [content]);

  useEffect(() => {
    if (!draft) return;
    setContent(draft.value);
    setCaret(draft.value.length);
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
    if (!commandState) return;
    const insert = `${command.slash}${command.usage === command.slash ? "" : " "}`;
    const next = content.slice(0, commandState.trigger.start)
      + insert
      + content.slice(commandState.trigger.end);
    setContent(next);
    setCaret(commandState.trigger.start + insert.length);
    setCommandMenuOpen(false);
    requestAnimationFrame(() => {
      textarea.current?.focus();
      textarea.current?.setSelectionRange(
        commandState.trigger.start + insert.length,
        commandState.trigger.start + insert.length,
      );
    });
  };

  const addFiles = useCallback(async (files: FileList | File[]) => {
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
  }, [allowImages]);

  useEffect(() => {
    const hasFiles = (event: DragEvent) => event.dataTransfer?.types.includes("Files") ?? false;
    const reset = () => {
      dragDepth.current = 0;
      setDragActive(false);
    };
    const onDragEnter = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      dragDepth.current += 1;
      setDragActive(true);
    };
    const onDragOver = (event: DragEvent) => {
      if (!hasFiles(event) || !event.dataTransfer) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = disabled ? "none" : "copy";
    };
    const onDragLeave = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragActive(false);
    };
    const onDrop = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      reset();
      if (!disabled && event.dataTransfer) void addFiles(event.dataTransfer.files);
    };
    document.addEventListener("dragenter", onDragEnter);
    document.addEventListener("dragover", onDragOver);
    document.addEventListener("dragleave", onDragLeave);
    document.addEventListener("drop", onDrop);
    window.addEventListener("dragend", reset);
    return () => {
      document.removeEventListener("dragenter", onDragEnter);
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
      window.removeEventListener("dragend", reset);
    };
  }, [addFiles, disabled]);

  return (
    <div className="composer-wrap">
      {dragActive && <DropOverlay disabled={disabled} />}
      {attachmentPreview?.preview && (
        <ImageLightbox
          src={attachmentPreview.preview}
          alt={attachmentPreview.name}
          onClose={closeAttachmentPreview}
        />
      )}
      <div className="composer">
        <CommandTriggerMenu
          commands={suggestions}
          selectedIndex={commandIndex}
          onPick={completeCommand}
        />
        {attachments.length > 0 && (
          <div className="composer-images">
            {attachments.map((attachment, index) => (
              <div className="composer-image" key={`${attachment.name}-${index}`} title={attachment.name}>
                {attachment.preview ? (
                  <button type="button" className="composer-image-preview" aria-label={`Preview ${attachment.name}`} onClick={() => setAttachmentPreview(attachment)}>
                    <img src={attachment.preview} alt={attachment.name} />
                  </button>
                ) : <FileText size={24} />}
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
            setCaret(event.target.selectionStart);
            setCommandMenuOpen(true);
          }}
          onSelect={(event) => {
            setCaret(event.currentTarget.selectionStart);
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
          <ContextMeter usage={usage} contextWindow={contextWindow} />
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
