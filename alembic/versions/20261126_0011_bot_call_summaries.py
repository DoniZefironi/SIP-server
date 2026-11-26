"""Поставить в очередь выжимки для сохранённых разговоров с Асей."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        UPDATE recording_jobs AS job
        SET summary_status = 'pending', summary_next_attempt_at = now()
        FROM calls
        WHERE job.call_id = calls.id
          AND calls.outcome = 'bot_answered'
          AND job.audio_kind = 'mix'
          AND job.summary_status = 'skipped'
          AND job.bitrix_routing_status = 'skipped'
          AND (
            job.transcription_status IN ('pending', 'submitting', 'processing')
            OR (job.transcription_status = 'completed' AND job.transcript ~ '[^[:space:]]')
          )
    """)


def downgrade() -> None:
    # Готовые выжимки остаются в карточках, ожидающие задачи возвращаются в skipped.
    op.execute("""
        UPDATE recording_jobs AS job
        SET summary_status = 'skipped'
        FROM calls
        WHERE job.call_id = calls.id
          AND calls.outcome = 'bot_answered'
          AND job.audio_kind = 'mix'
          AND job.summary_status = 'pending'
          AND job.bitrix_routing_status = 'skipped'
    """)
