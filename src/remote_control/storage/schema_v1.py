from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text

# Immutable snapshot of database schema version 1.
#
# Do not derive this metadata from the current ORM models. New tables/columns must
# be introduced only by a new migration so a fresh database and an upgraded
# database always traverse the same schema history.
V1_METADATA = MetaData()

Table(
    "jobs",
    V1_METADATA,
    Column("id", String(64), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("requested_by_channel", String(64), nullable=False),
    Column("requested_by_user", String(128), nullable=False, index=True),
    Column("requested_host", String(128), nullable=False),
    Column("assigned_host", String(128), nullable=True),
    Column("instruction", Text, nullable=False),
    Column("state", String(32), nullable=False, index=True),
    Column("external_session_id", String(255), nullable=True),
    Column("pid", Integer, nullable=True),
    Column("result", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Table(
    "events",
    V1_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", String(64), nullable=False, index=True),
    Column("job_id", String(64), nullable=True, index=True),
    Column("project_id", String(128), nullable=True, index=True),
    Column("host_id", String(128), nullable=True, index=True),
    Column("payload_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

Table(
    "hosts",
    V1_METADATA,
    Column("id", String(128), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("os", String(64), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("capabilities_json", Text, nullable=False),
    Column("last_heartbeat", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Table(
    "project_sessions",
    V1_METADATA,
    Column("id", String(64), primary_key=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("owner_user_id", String(128), nullable=False, index=True),
    Column("external_session_id", String(255), nullable=True, index=True),
    Column("host_id", String(128), nullable=True, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("last_job_id", String(64), nullable=True, index=True),
    Column("locked_by_job_id", String(64), nullable=True, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_active_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
)

Table(
    "sessions",
    V1_METADATA,
    Column("id", String(64), primary_key=True),
    Column("job_id", String(64), nullable=False, unique=True, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("host_id", String(128), nullable=False, index=True),
    Column("agent_type", String(64), nullable=False),
    Column("external_session_id", String(255), nullable=False, index=True),
    Column("status", String(32), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_active_at", DateTime(timezone=True), nullable=False),
)

Table(
    "approvals",
    V1_METADATA,
    Column("id", String(64), primary_key=True),
    Column("job_id", String(64), nullable=False, index=True),
    Column("requested_by_user", String(128), nullable=False, index=True),
    Column("approval_type", String(64), nullable=False),
    Column("question", Text, nullable=False),
    Column("details", Text, nullable=True),
    Column("options_json", Text, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("selected_option", String(64), nullable=True),
    Column("response_text", Text, nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

Table(
    "recovery",
    V1_METADATA,
    Column("job_id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False, index=True),
    Column("mode", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("next_retry_at", DateTime(timezone=True), nullable=True, index=True),
    Column("execution_id", String(128), nullable=True, index=True),
    Column("resume_instruction", Text, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Table(
    "project_work",
    V1_METADATA,
    Column("job_id", String(64), primary_key=True),
    Column("adapter", String(64), nullable=False, index=True),
    Column("host_id", String(128), nullable=True, index=True),
    Column("task_id", String(128), nullable=True, index=True),
    Column("role", String(64), nullable=True),
    Column("status", String(32), nullable=False, index=True),
    Column("result_path", Text, nullable=True),
    Column("next_task_id", String(128), nullable=True),
    Column("error", Text, nullable=True),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("submitted_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Table(
    "telegram_project_topics",
    V1_METADATA,
    Column("user_id", String(128), primary_key=True),
    Column("project_id", String(128), primary_key=True),
    Column("chat_id", String(128), nullable=False, index=True),
    Column("message_thread_id", Integer, nullable=False, index=True),
    Column("topic_name", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

Table(
    "telegram_message_bindings",
    V1_METADATA,
    Column("chat_id", String(128), primary_key=True),
    Column("message_id", Integer, primary_key=True),
    Column("message_thread_id", Integer, nullable=True, index=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("project_id", String(128), nullable=False, index=True),
    Column("job_id", String(64), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
