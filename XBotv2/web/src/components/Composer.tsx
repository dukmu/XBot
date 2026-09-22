import { FileText, Paperclip, Square, SquareSlash, Send, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { CommandInfo, ImageInput, UsageData } from "../api/types";
import { ContextMeter } from "./ContextMeter";
import { CommandTriggerMenu } from "./CommandTriggerMenu";
import { DropOverlay } from "./DropOverlay";
import { ImageLightbox } from "./ImageLightbox";
import { commandSuggestions } from "../commands";

// Ported composer hint: the whole-queue steer gesture is advertised by copy
// rather than by a control of its own.
export const STEER_QUEUE_PLACEHOLDER = "Cmd/Ctrl+Enter steers all queued messages";
export const COMPOSER_PLACEHOLDER = "Message XBot · paste images";

export interface PendingAttachment extends ImageInput {
  name: string;
  preview?: string;
}

interface ComposerProps {
  running: boolean;
  disabled: boolean;
  commands: CommandInfo[];
  /** Ported composer chrome: mode/model selectors above, controls below. */
  onOpenCommands?: () => void;
  accessMode?: ReactNode;
  runtimeControls?: ReactNode;
  draft: { id: number; value: string } | null;
  allowImages: boolean;
  usage: UsageData;
  contextWindow: number;
  onSend: (content: string, attachments: PendingAttachment[]) => Promise<boolean>;
  inputHistory: string[];
  onSubmitted: (content: string) => void;
  onInterrupt: () => Promise<void>;
  /** Queued (next-turn) messages: the whole-queue steer gesture targets these. */
  pendingCount?: number;
  onSteerAll?: () => void;
}

export function Composer({ running, disabled, commands, draft, allowImages, usage, contextWindow, onSend, inputHistory, onSubmitted, onInterrupt, onOpenCommands, accessMode, runtimeControls, pendingCount = 0, onSteerAll }: ComposerProps) {
  const [content, setContent] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [commandIndex, setCommandIndex] = useState(0);
  const [commandMenuOpen, setCommandMenuOpen] = useState(true);
  const [caret, setCaret] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [attachmentPreview, setAttachmentPreview] = useState<PendingAttachment | null>(null);
  const [historyIndex, setHistoryIndex] = useState<number | null>(null);
  const historyDraft = useRef("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const mirror = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const closeAttachmentPreview = useCallback(() => setAttachmentPreview(null), []);
  const commandState = commandMenuOpen ? commandSuggestions(commands, content, caret) : null;
  const suggestions = commandState?.commands.slice(0, 9) ?? [];
  // The gesture needs an empty draft in a running ordinary session, and XBot
  // also requires something to steer, so the hint never names a no-op.
  const canSteerQueue = !disabled
    && running
    && pendingCount > 0
    && onSteerAll !== undefined
    && !suggestions.length
    && !content.trim()
    && attachments.length === 0;

  // The draft's height comes from the hidden mirror in normal flow, and the
  // textarea rides it inside one scrollport (ported geometry): the textarea is
  // never a scroller of its own, so its glyphs and the caret share one offset.
  const revealCaret = useCallback((index: number) => {
    const scrollEl = scroll.current;
    const mirrorEl = mirror.current;
    const text = mirrorEl?.firstChild;
    if (!scrollEl || !mirrorEl || !(text instanceof Text)) return;
    // A box that cannot scroll has nothing to reveal.
    if (scrollEl.scrollHeight <= scrollEl.clientHeight) return;
    const at = Math.min(index, text.data.length);
    // A caret straight after a newline sits on an empty line: measure the
    // newline itself and step one line down, which every engine agrees on.
    const afterNewline = at > 0 && text.data[at - 1] === "\n";
    const range = document.createRange();
    range.setStart(text, afterNewline ? at - 1 : at);
    if (afterNewline) range.setEnd(text, at);
    else range.collapse(true);
    const line = afterNewline ? Number.parseFloat(window.getComputedStyle(mirrorEl).lineHeight) || 0 : 0;
    const rect = range.getBoundingClientRect();
    const box = scrollEl.getBoundingClientRect();
    if (rect.bottom + line > box.bottom) scrollEl.scrollTop += rect.bottom + line - box.bottom;
    else if (rect.top + line < box.top) scrollEl.scrollTop -= box.top - rect.top - line;
  }, []);

  useEffect(() => {
    revealCaret(caret);
  }, [caret, content, revealCaret]);

  useEffect(() => setCommandIndex(0), [content]);

  useEffect(() => {
    if (!draft) return;
    setContent(draft.value);
    setHistoryIndex(null);
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
    } else if (value) {
      onSubmitted(value);
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
        {runtimeControls}
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
        <div className="composer-draft-scroll" ref={scroll}>
          <div className="composer-draft-grow">
            <div ref={mirror} aria-hidden className="composer-draft-mirror" data-input-mirror>{`${content}\n`}</div>
            <textarea
              ref={textarea}
              value={content}
              disabled={disabled}
              rows={1}
          placeholder={canSteerQueue ? STEER_QUEUE_PLACEHOLDER : COMPOSER_PLACEHOLDER}
          aria-label="Message XBot"
          onChange={(event) => {
            setContent(event.target.value);
            setHistoryIndex(null);
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
            if (!suggestions.length && event.key === "ArrowUp" && inputHistory.length) {
              const atStart = event.currentTarget.selectionStart === 0;
              if (atStart || !content) {
                event.preventDefault();
                const next = historyIndex === null
                  ? inputHistory.length - 1
                  : Math.max(0, historyIndex - 1);
                if (historyIndex === null) historyDraft.current = content;
                setHistoryIndex(next);
                setContent(inputHistory[next]);
                requestAnimationFrame(() => {
                  const element = textarea.current;
                  element?.setSelectionRange(element.value.length, element.value.length);
                });
                return;
              }
            }
            if (!suggestions.length && event.key === "ArrowDown" && historyIndex !== null) {
              event.preventDefault();
              if (historyIndex >= inputHistory.length - 1) {
                setHistoryIndex(null);
                setContent(historyDraft.current);
              } else {
                const next = historyIndex + 1;
                setHistoryIndex(next);
                setContent(inputHistory[next]);
              }
              return;
            }
            if (suggestions.length && event.key === "Tab") {
              event.preventDefault();
              completeCommand(suggestions[commandIndex]);
              return;
            }
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              // Held-down Enter must not machine-gun sends or steers.
              if (event.repeat) return;
              // Accelerated Enter on an empty draft acts on the queue, not on
              // the (empty) draft: it steers every still-queued message.
              if ((event.ctrlKey || event.metaKey) && canSteerQueue) {
                onSteerAll?.();
                return;
              }
              void submit();
            }
          }}
            />
          </div>
        </div>
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
          <button
            className="composer-tool"
            type="button"
            title="Commands"
            aria-label="Commands"
            disabled={disabled}
            onClick={() => onOpenCommands?.()}
          >
            <SquareSlash size={15} />
          </button>
          {accessMode}
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
