/*
 * Adapted from DeepSeek Harness AppFrame.tsx (MIT).
 * The frame is intentionally React-state-only: XBot's session/runtime state
 * remains behind its public client API rather than importing DSh's Cordis
 * runtime or creating a second transport model.
 */
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import css from "./DshAppFrame.module.css";

const SIDEBAR_DEFAULT = 280;
const SIDEBAR_MOBILE_MAX = 264;
const SIDEBAR_MOBILE_MIN = 236;
const SIDEBAR_MIN = 264;
const SIDEBAR_MAX = 420;
const SIDEBAR_COLLAPSED = 56;
const SIDEBAR_AUTO_COLLAPSE = 1024;
const SIDEBAR_STORAGE_KEY = "xbot.sidebar.width";

interface DshAppFrameProps {
  mobileSidebarOpen: boolean;
  sidebar: (layout: { collapsed: boolean; width: number; toggle: () => void }) => ReactNode;
  children: ReactNode;
}

function initialSidebarWidth(): number {
  const raw = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
  if (raw === null) return SIDEBAR_DEFAULT;
  const stored = Number(raw);
  if (stored === 0) return 0;
  if (Number.isFinite(stored) && stored >= SIDEBAR_MIN && stored <= SIDEBAR_MAX) return stored;
  return SIDEBAR_DEFAULT;
}

function clampSidebar(width: number): number {
  return Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, Math.round(width)));
}

export function DshAppFrame({ mobileSidebarOpen, sidebar, children }: DshAppFrameProps) {
  const frame = useRef<HTMLDivElement>(null);
  const [viewport, setViewport] = useState(() => window.innerWidth);
  const [preference, setPreference] = useState(initialSidebarWidth);
  const [narrowExpanded, setNarrowExpanded] = useState(false);
  const [dragging, setDragging] = useState(false);
  const dragOrigin = useRef(0);
  const dragWidth = useRef(0);
  const narrow = viewport < SIDEBAR_AUTO_COLLAPSE;
  const mobile = viewport <= 760;
  const collapsed = !mobile && (narrow ? !narrowExpanded : preference === 0);
  const mobileWidth = Math.min(SIDEBAR_MOBILE_MAX, Math.max(SIDEBAR_MOBILE_MIN, Math.round(viewport * 0.84)));
  const width = mobile ? mobileWidth : collapsed ? SIDEBAR_COLLAPSED : (preference || SIDEBAR_DEFAULT);

  useEffect(() => {
    const element = frame.current;
    if (!element) return;
    let animationFrame: number | null = null;
    const observer = new ResizeObserver(() => {
      if (animationFrame !== null) return;
      animationFrame = requestAnimationFrame(() => {
        animationFrame = null;
        const next = element.getBoundingClientRect().width;
        if (next > 0) setViewport(next);
      });
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      if (animationFrame !== null) cancelAnimationFrame(animationFrame);
    };
  }, []);

  useEffect(() => setNarrowExpanded(false), [narrow]);

  const toggle = useCallback(() => {
    if (narrow) {
      setNarrowExpanded((expanded) => !expanded);
      return;
    }
    setPreference((current) => {
      const next = current === 0 ? SIDEBAR_DEFAULT : 0;
      window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
      return next;
    });
  }, [narrow]);

  return (
    <div
      ref={frame}
      className={css.frame}
      data-dragging={dragging || undefined}
      data-sidebar-collapsed={collapsed || undefined}
      data-sidebar-open={mobileSidebarOpen || undefined}
      style={{ gridTemplateColumns: `${width}px minmax(0, 1fr)` }}
    >
      <div className={css.sidebar}>{sidebar({ collapsed, width, toggle })}</div>
      <div className={css.center}>{children}</div>
      {!collapsed && (
        <div
          className={css.handle}
          data-dragging={dragging || undefined}
          style={{ left: width }}
          onPointerDown={(event) => {
            event.preventDefault();
            event.currentTarget.setPointerCapture(event.pointerId);
            dragOrigin.current = event.clientX;
            dragWidth.current = width;
            setDragging(true);
          }}
          onPointerMove={(event) => {
            if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
            setPreference(clampSidebar(dragWidth.current + event.clientX - dragOrigin.current));
          }}
          onPointerUp={(event) => {
            if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
            event.currentTarget.releasePointerCapture(event.pointerId);
            const next = clampSidebar(dragWidth.current + event.clientX - dragOrigin.current);
            setPreference(next);
            window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next));
            setDragging(false);
          }}
        />
      )}
    </div>
  );
}
