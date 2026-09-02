/* Artifact lane adapted from DeepSeek Harness ProducedFiles presentation (MIT). */
import { FileDown } from "lucide-react";
import type { JsonObject } from "../api/types";
import styles from "./ToolArtifacts.module.css";

interface ArtifactLink {
  id: string;
  name: string;
  url: string;
  mediaType: string;
  size: number | null;
}

export function ToolArtifacts({ artifacts }: { artifacts: JsonObject[] }) {
  const links = artifacts.map(artifactLink).filter((value): value is ArtifactLink => value !== null);
  if (links.length === 0) return null;
  return (
    <section className={styles.root} aria-label="Tool artifacts">
      <span className={styles.label}>Artifacts</span>
      <div className={styles.files}>
        {links.map((artifact) => (
          <a
            key={artifact.id}
            className={styles.file}
            href={artifact.url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open artifact ${artifact.name}`}
            title={artifact.id}
          >
            <FileDown size={14} />
            <span>
              <b>{artifact.name}</b>
              <small>{artifactMeta(artifact)}</small>
            </span>
          </a>
        ))}
      </div>
    </section>
  );
}

function artifactLink(value: JsonObject): ArtifactLink | null {
  const id = typeof value.id === "string" ? value.id : "";
  const url = typeof value.url === "string" ? value.url : "";
  if (!id || !url) return null;
  return {
    id,
    url,
    name: typeof value.name === "string" && value.name ? value.name : id,
    mediaType: typeof value.media_type === "string" ? value.media_type : "",
    size: typeof value.size === "number" && value.size >= 0 ? value.size : null,
  };
}

function artifactMeta(artifact: ArtifactLink): string {
  return [artifact.mediaType, artifact.size === null ? "" : formatBytes(artifact.size)]
    .filter(Boolean)
    .join(" · ");
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} kB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
