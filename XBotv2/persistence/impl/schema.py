"""Backend-owned bookkeeping, without business-domain tables."""

COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_kv(thread_id TEXT NOT NULL, namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(thread_id,namespace,key));
CREATE TABLE IF NOT EXISTS persistence_tables(table_name TEXT PRIMARY KEY, owner TEXT NOT NULL);
"""
ROLLOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS records(stream_id TEXT NOT NULL, seq INTEGER NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, PRIMARY KEY(stream_id,seq), UNIQUE(stream_id,record_id));
CREATE TABLE IF NOT EXISTS stream_heads(stream_id TEXT PRIMARY KEY, indexed_seq INTEGER NOT NULL DEFAULT 0, indexed_offset INTEGER NOT NULL DEFAULT 0, indexed_mtime_ns INTEGER NOT NULL DEFAULT 0);
"""

from XBotv2.persistence.impl.record_artifacts import SCHEMA as ARTIFACT_SCHEMA

COMMON_SCHEMA += ARTIFACT_SCHEMA
