/*
 * Vendored from deepseek-harness packages/client/ui-primitives/src/markdown/render.tsx
 * (dsh 0.1.0-rc.7, MIT): `sanitizeUrl` and `remoteImageUrl`, verbatim. Keep them
 * in sync with that path instead of editing them here.
 */

/** Allow only the protocols a rendered document may navigate to. */
export function sanitizeUrl(url: string): string {
  try {
    switch (new URL(url).protocol) {
      case "http:":
      case "https:":
      case "mailto:":
        return url;
      default:
        return "";
    }
  } catch {
    // Relative and otherwise unparsable destinations are disallowed alongside
    // disallowed protocols; new URL() has no other failure mode for strings.
    return "";
  }
}

/** Resolve the source of an image that may actually be fetched from a page. */
export function remoteImageUrl(url: string): string | undefined {
  try {
    const protocol = new URL(url).protocol;
    return protocol === "http:" || protocol === "https:" ? url : undefined;
  } catch {
    // Same single failure mode as above: not an absolute URL.
    return undefined;
  }
}
