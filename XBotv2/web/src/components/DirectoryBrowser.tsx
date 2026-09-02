/* Adapted from DeepSeek Harness DirectoryBrowser.tsx (MIT). */
import { ArrowUp, Check, ChevronRight, Eye, EyeOff, Folder, LoaderCircle, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DirectoryEntryData, DirectoryListingData } from "../api/types";
import css from "./DirectoryBrowser.module.css";

interface DirectoryBrowserProps {
  initialPath?: string;
  listDirectory: (path?: string, signal?: AbortSignal) => Promise<DirectoryListingData>;
  onOpen: (path: string) => void;
  onClose: () => void;
}

export function DirectoryBrowser({ initialPath, listDirectory, onOpen, onClose }: DirectoryBrowserProps) {
  const [listing, setListing] = useState<DirectoryListingData | null>(null);
  const [selected, setSelected] = useState<DirectoryEntryData | null>(null);
  const [path, setPath] = useState(initialPath || "");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  const entries = useMemo(
    () => listing?.entries.filter((entry) => showHidden || !entry.hidden) || [],
    [listing, showHidden],
  );

  const load = useCallback((target?: string) => {
    const sequence = ++request.current;
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setLoading(true);
    setError("");
    void listDirectory(target || undefined, controller.signal).then((value) => {
      if (sequence !== request.current) return;
      setListing(value);
      setPath(value.path);
      setSelected(null);
    }).catch((reason: unknown) => {
      if (sequence !== request.current || (reason instanceof DOMException && reason.name === "AbortError")) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (sequence === request.current) {
        activeRequest.current = null;
        setLoading(false);
      }
    });
  }, [listDirectory]);

  useEffect(() => {
    load(initialPath);
    return () => activeRequest.current?.abort();
  }, [initialPath, load]);

  const target = selected?.path || listing?.path || "";
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className={`dialog ${css.dialog}`} role="dialog" aria-modal="true" aria-labelledby="directory-browser-title" onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}>
        <header className={css.header}>
          <div className={css.titleRow}>
            <h2 id="directory-browser-title">Select workspace folder</h2>
            <button type="button" className="icon-button" aria-label="Close" title="Close" onClick={onClose}><X size={17} /></button>
          </div>
          <form className={css.pathBar} onSubmit={(event) => { event.preventDefault(); load(path); }}>
            <input aria-label="Directory path" value={path} onChange={(event) => setPath(event.target.value)} />
            <button type="submit" className="icon-button small" aria-label="Open path" title="Open path"><ChevronRight size={15} /></button>
          </form>
        </header>
        <div className={css.toolbar}>
          <button type="button" className="icon-button" aria-label="Parent folder" title="Parent folder" disabled={!listing?.parent || loading} onClick={() => load(listing?.parent || undefined)}><ArrowUp size={16} /></button>
          <button type="button" className="icon-button" aria-label="Home folder" title="Home folder" disabled={!listing || loading} onClick={() => load(listing?.home)}><Folder size={16} /></button>
          <span title={listing?.path}>{listing?.path || "Loading directories"}</span>
          <button type="button" className="icon-button" aria-label={showHidden ? "Hide hidden folders" : "Show hidden folders"} title={showHidden ? "Hide hidden folders" : "Show hidden folders"} onClick={() => setShowHidden((value) => !value)}>{showHidden ? <EyeOff size={16} /> : <Eye size={16} />}</button>
        </div>
        <div className={css.entries} role="listbox" aria-label="Folders">
          {loading && <div className={css.status} role="status"><LoaderCircle size={15} className="spin" /> Loading</div>}
          {!loading && error && <div className={css.error} role="alert">{error}</div>}
          {!loading && !error && entries.map((entry) => (
            <div className={`${css.entry} ${selected?.path === entry.path ? css.selected : ""}`} key={entry.path}>
              <button type="button" role="option" aria-selected={selected?.path === entry.path} onClick={() => setSelected(entry)} onDoubleClick={() => load(entry.path)}>
                <Folder size={16} /> <span>{entry.name}</span>
              </button>
              <button type="button" className="icon-button small" aria-label={`Open ${entry.name}`} title={`Open ${entry.name}`} onClick={() => load(entry.path)}><ChevronRight size={14} /></button>
            </div>
          ))}
          {!loading && !error && !entries.length && <div className={css.status}>No folders</div>}
        </div>
        {listing?.truncated && <div className={css.note}>Showing the first 500 folders</div>}
        <footer className={css.footer}>
          <span>{selected?.name || (listing ? "Current folder" : "")}</span>
          <button type="button" className="secondary-button" onClick={onClose}>Cancel</button>
          <button type="button" className="primary-button" disabled={!target || loading} onClick={() => onOpen(target)}><Check size={15} /> Select</button>
        </footer>
      </section>
    </div>
  );
}
