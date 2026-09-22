/* Image rendering adapted from DeepSeek Harness ui-primitives/markdown/render.tsx (MIT). */
import type { ComponentPropsWithoutRef } from "react";
import styles from "./MarkdownImage.module.css";
import { remoteImageUrl } from "./mediaPolicy";

/**
 * Ported markdown image policy: only an absolute `http(s)` source is fetched.
 *
 * Anything else — a relative path, a `data:` or `file:` URL — is not a resource
 * the page may load, so it renders as its alt text instead of a broken image.
 */
export function MarkdownImage({ src, alt }: ComponentPropsWithoutRef<"img">) {
  const source = remoteImageUrl(src ?? "");
  if (source === undefined) {
    return <span className={styles.imageAlt}>{alt}</span>;
  }
  return (
    <img
      className={styles.image}
      src={source}
      alt={alt}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
    />
  );
}
