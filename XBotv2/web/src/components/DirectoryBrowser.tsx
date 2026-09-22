/* Adapted from DeepSeek Harness ui-directory-picker-browse DirectoryBrowser.tsx
 * (MIT): the same heading, breadcrumb, click-to-edit path zone, folder lists and
 * footer labels, over XBot's own directory-listing API. */
import { Check, ChevronRight, Eye, EyeOff, Folder, LoaderCircle, Pencil } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { DirectoryEntryData, DirectoryListingData } from "../api/types";
import css from "./DirectoryBrowser.module.css";

interface DirectoryBrowserProps {
  initialPath?: string;
  listDirectory: (path?: string, signal?: AbortSignal) => Promise<DirectoryListingData>;
  onOpen: (path: string) => void;
  onClose: () => void;
}

/** Ported copy. `New folder` has no XBot operation behind it, so it is absent. */
export const DIRECTORY_TITLE = "Select Workspace Directory";
export const EDIT_PATH = "Edit path";
export const SHOW_HIDDEN = "Show hidden files";
export const HIDE_HIDDEN = "Hide hidden files";
export const CANCEL = "Cancel";
export const OPEN = "Open";
export const HOME_CRUMB = "Home";

export interface Crumb {
  name: string;
  path: string;
}

/**
 * Breadcrumb rows for a path: inside the home subtree the chain starts at
 * `Home`; outside it the ancestry shows, the root labelled by its own path. The
 * last crumb names the path itself, as the ported header does.
 */
export function directoryCrumbs(listing: Pick<DirectoryListingData, "path" | "home" | "separator">): Crumb[] {
  const { path, home, separator } = listing;
  const root = separator === "\\" ? path.split("\\")[0] + separator : separator;
  if (path === home) return [{ name: HOME_CRUMB, path: home }];
  if (home && path.startsWith(home.endsWith(separator) ? home : home + separator)) {
    const tail = path.slice(home.length).split(separator).filter(Boolean);
    const crumbs: Crumb[] = [{ name: HOME_CRUMB, path: home }];
    let current = home;
    for (const segment of tail) {
      current = current.endsWith(separator) ? current + segment : current + separator + segment;
      crumbs.push({ name: segment, path: current });
    }
    return crumbs;
  }
  const segments = path.split(separator).filter(Boolean);
  const crumbs: Crumb[] = [{ name: root, path: root }];
  let current = root;
  for (const segment of segments) {
    current = current.endsWith(separator) ? current + segment : current + separator + segment;
    crumbs.push({ name: segment, path: current });
  }
  return crumbs;
}

export function DirectoryBrowser({ initialPath, listDirectory, onOpen, onClose }: DirectoryBrowserProps) {
  const [listing, setListing] = useState<DirectoryListingData | null>(null);
  const [selected, setSelected] = useState<DirectoryEntryData | null>(null);
  const [children, setChildren] = useState<DirectoryEntryData[]>([]);
  const [childPath, setChildPath] = useState("");
  const [path, setPath] = useState(initialPath || "");
  const [editing, setEditing] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);

  const visible = useCallback(
    (entries: DirectoryEntryData[]) => entries.filter((entry) => showHidden || !entry.hidden),
    [showHidden],
  );
  const entries = useMemo(() => visible(listing?.entries || []), [listing, visible]);
  const childEntries = useMemo(() => visible(children), [children, visible]);

  const load = useCallback((target?: string) => {
    const sequence = ++request.current;
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setLoading(true);
    setError("");
    setSelected(null);
    setChildren([]);
    setChildPath("");
    void listDirectory(target || undefined, controller.signal).then((value) => {
      if (sequence !== request.current) return;
      setListing(value);
      setPath(value.path);
      setEditing(false);
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

  // Selecting a level's row shows that folder's own children beside it, the
  // ported two-column view.
  const select = useCallback((entry: DirectoryEntryData) => {
    setSelected(entry);
    setChildren([]);
    setChildPath("");
    void listDirectory(entry.path).then((value) => {
      setChildren(value.entries);
      setChildPath(value.path);
    }).catch(() => {
      // A folder the host will not list (permissions, disappeared) stays
      // selected; Open still targets it.
      setChildren([]);
      setChildPath("");
    });
  }, [listDirectory]);

  const target = selected?.path || listing?.path || "";
  // The breadcrumb names the path the browser would open — the selection when
  // there is one, else the listed level — as the ported header does.
  const crumbs = useMemo(
    () => (listing && target ? directoryCrumbs({ ...listing, path: target }) : []),
    [listing, target],
  );

  // One level of folders: the ported row is a single button carrying the folder
  // glyph, the name and a decorative chevron. In the current level a click
  // selects the folder (its children appear beside it); in the child level a
  // click descends, which is how the ported two-column view shifts.
  const level = (list: DirectoryEntryData[], label: string, descend: boolean): ReactNode => (
    <ul className={css.list} aria-label={label}>
      {list.map((entry) => (
        <li key={entry.path}>
          <button
            type="button"
            className={!descend && selected?.path === entry.path ? "selected" : ""}
            aria-current={!descend && selected?.path === entry.path ? "true" : undefined}
            onClick={() => {
              if (descend) load(entry.path);
              else select(entry);
            }}
          >
            <Folder size={16} aria-hidden />
            <span className={css.entryName}>{entry.name}</span>
            <ChevronRight size={14} className={css.entryChevron} aria-hidden />
          </button>
        </li>
      ))}
    </ul>
  );

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose();
    }}>
      <section className={`dialog ${css.dialog}`} role="dialog" aria-modal="true" aria-labelledby="directory-browser-title" onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}>
        <header className={css.header}>
          <h2 id="directory-browser-title">{DIRECTORY_TITLE}</h2>
          <nav className={css.crumbs} aria-label="Directory">
            {crumbs.map((crumb, index) => (
              <span key={crumb.path}>
                {index > 0 && <ChevronRight size={12} aria-hidden />}
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => load(crumb.path)}
                >
                  {crumb.name}
                </button>
              </span>
            ))}
            <button
              type="button"
              className={`icon-button small ${css.editPath}`}
              aria-label={EDIT_PATH}
              title={EDIT_PATH}
              aria-expanded={editing}
              onClick={() => {
                setPath(listing ? `${listing.path}${listing.separator}` : path);
                setEditing((value) => !value);
              }}
            >
              <Pencil size={13} />
            </button>
          </nav>
          {editing && (
            <form className={css.pathBar} onSubmit={(event) => {
              event.preventDefault();
              load(path);
            }}>
              <input
                autoFocus
                aria-label="Directory path"
                value={path}
                onChange={(event) => setPath(event.target.value)}
              />
              <button type="submit" className="icon-button small" aria-label="Open path" title="Open path">
                <ChevronRight size={15} />
              </button>
            </form>
          )}
        </header>
        <div className={css.panes}>
          {loading && <div className={css.status} role="status"><LoaderCircle size={15} className="spin" /> Loading</div>}
          {!loading && error && <div className={css.error} role="alert">{error}</div>}
          {!loading && !error && (
            <>
              {level(entries, "Folders", false)}
              {selected && (
                <div className={css.children}>
                  <span className={css.childrenTitle} title={childPath}>{selected.name}</span>
                  {level(childEntries, `Folders in ${selected.name}`, true)}
                </div>
              )}
            </>
          )}
        </div>
        {listing?.truncated && <div className={css.note}>Showing the first 500 folders</div>}
        <footer className={css.footer}>
          <button
            type="button"
            className={css.hiddenToggle}
            aria-pressed={showHidden}
            onClick={() => setShowHidden((value) => !value)}
          >
            {showHidden ? <EyeOff size={14} aria-hidden /> : <Eye size={14} aria-hidden />}
            {showHidden ? HIDE_HIDDEN : SHOW_HIDDEN}
          </button>
          <span>{selected?.name || (listing ? "Current folder" : "")}</span>
          <button type="button" className="secondary-button" onClick={onClose}>{CANCEL}</button>
          <button type="button" className="primary-button" disabled={!target || loading} onClick={() => onOpen(target)}><Check size={15} /> {OPEN}</button>
        </footer>
      </section>
    </div>
  );
}
