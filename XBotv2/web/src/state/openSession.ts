/**
 * Remember the session this browser had open so a reload can resume it.
 *
 * The server replays pending interactions on a resume response, so restoring
 * the open session is what makes an unanswered approval reachable again.
 */

const STORAGE_KEY = "xbotv2.openSession";

export interface OpenSessionRef {
  session_id: string;
  thread_id: string;
  workspace_root: string;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readOpenSession(): OpenSessionRef | null {
  const store = storage();
  if (!store) return null;
  let raw: string | null = null;
  try {
    raw = store.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const candidate = value as Record<string, unknown>;
    const sessionId = String(candidate.session_id || "");
    const threadId = String(candidate.thread_id || "");
    if (!sessionId || !threadId) return null;
    return {
      session_id: sessionId,
      thread_id: threadId,
      workspace_root: String(candidate.workspace_root || ""),
    };
  } catch {
    return null;
  }
}

export function writeOpenSession(session: OpenSessionRef): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify({
      session_id: session.session_id,
      thread_id: session.thread_id,
      workspace_root: session.workspace_root,
    }));
  } catch {
    // A browser that refuses storage must not break session navigation.
  }
}

export function clearOpenSession(): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(STORAGE_KEY);
  } catch {
    // Ignore an unavailable storage backend.
  }
}
