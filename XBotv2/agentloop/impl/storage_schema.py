"""Rebuildable Agentloop projections; bodies live only in rollout."""

TABLES = (
    "message_refs",
    "surface",
    "transcript",
    "lineage",
    "inbox",
    "input_ids",
    "turn_starts",
    "open_transactions",
    "history_state",
)
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS message_refs(stream_id TEXT NOT NULL,message_id TEXT NOT NULL,record_id TEXT NOT NULL,ordinal INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(stream_id,message_id))",
    "CREATE TABLE IF NOT EXISTS surface(thread_id TEXT NOT NULL,order_key INTEGER NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(thread_id,order_key),UNIQUE(thread_id,record_id))",
    "CREATE TABLE IF NOT EXISTS transcript(thread_id TEXT NOT NULL,order_key INTEGER NOT NULL,record_id TEXT NOT NULL,PRIMARY KEY(thread_id,order_key),UNIQUE(thread_id,record_id))",
    "CREATE TABLE IF NOT EXISTS lineage(thread_id TEXT NOT NULL,summary_id TEXT NOT NULL,order_key INTEGER NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(thread_id,summary_id,order_key))",
    "CREATE TABLE IF NOT EXISTS inbox(input_id TEXT NOT NULL,thread_id TEXT NOT NULL,delivery TEXT NOT NULL,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,body_ref TEXT NOT NULL,order_key INTEGER NOT NULL,PRIMARY KEY(thread_id,input_id))",
    "CREATE TABLE IF NOT EXISTS input_ids(thread_id TEXT NOT NULL,input_id TEXT NOT NULL,accepted_record TEXT NOT NULL,current_record TEXT NOT NULL,retired INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(thread_id,input_id))",
    "CREATE TABLE IF NOT EXISTS turn_starts(thread_id TEXT NOT NULL,input_id TEXT NOT NULL,PRIMARY KEY(thread_id,input_id))",
    "CREATE TABLE IF NOT EXISTS open_transactions(thread_id TEXT NOT NULL,kind TEXT NOT NULL,transaction_id TEXT NOT NULL,PRIMARY KEY(thread_id,kind,transaction_id))",
    "CREATE TABLE IF NOT EXISTS history_state(thread_id TEXT PRIMARY KEY,turn_count INTEGER NOT NULL DEFAULT 0,surface_version INTEGER NOT NULL DEFAULT 0,surface_revision INTEGER NOT NULL DEFAULT 0,transcript_revision INTEGER NOT NULL DEFAULT 0)",
)
