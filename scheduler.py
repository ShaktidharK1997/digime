"""Scheduler — background thread for proactive work.

Runs alongside the Slack bot and handles:
- Morning digest: at the configured local time, runs the orchestrator with
  the digest prompt and DMs the result to the owner.
- Auto-archive: active threads untouched for N days get summarized and
  marked complete, so memory doesn't depend on remembering to say "done".
- Housekeeping: expires stale pending confirmation actions.

State (last digest date) lives in the memory store's meta table so restarts
don't double-fire.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import orchestrator
from tools import memory_store
from tools.slack_tool import MessagePoster
from tools.summarizer import generate_conversation_gist

logger = logging.getLogger(__name__)

PROFILE_PATH = Path(__file__).resolve().parent / "config" / "profile.yaml"
DIGEST_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "digest_prompt.txt"

CHECK_INTERVAL_SECONDS = 30
MAINTENANCE_INTERVAL_SECONDS = 3600
# If the bot was down past the scheduled time, still send the digest within
# this window; beyond it, skip until tomorrow (a "morning" digest at 6pm is
# just noise).
DIGEST_GRACE_HOURS = 4
PENDING_ACTION_TTL_HOURS = 24

DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_DIGEST_TIME = "08:00"
DEFAULT_AUTO_ARCHIVE_DAYS = 3


class Scheduler(threading.Thread):
    """Daemon thread; safe to start before the Slack handler connects."""

    def __init__(self, slack_client, owner_user_id: str | None):
        super().__init__(daemon=True, name="digime-scheduler")
        self.client = slack_client
        self.owner_user_id = owner_user_id
        self._last_maintenance = 0.0
        self._warned_no_owner = False

    def run(self) -> None:
        logger.info("Scheduler started (digest owner: %s)", self.owner_user_id or "unset")
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    def _tick(self) -> None:
        config = _load_profile()
        self._maybe_run_digest(config)

        now = time.time()
        if now - self._last_maintenance >= MAINTENANCE_INTERVAL_SECONDS:
            self._last_maintenance = now
            self._run_maintenance(config)

    # ------------------------------------------------------------------
    # Morning digest
    # ------------------------------------------------------------------

    def _maybe_run_digest(self, config: dict) -> None:
        digest_cfg = config.get("digest") or {}
        if not digest_cfg.get("enabled", False):
            return

        if not self.owner_user_id:
            if not self._warned_no_owner:
                logger.warning(
                    "Digest is enabled but SLACK_ALLOWED_USER_ID is not set — "
                    "I don't know whose DM to post to. Digest disabled."
                )
                self._warned_no_owner = True
            return

        tz_name = (config.get("location") or {}).get("timezone", DEFAULT_TIMEZONE)
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo(DEFAULT_TIMEZONE)

        now_local = datetime.now(tz)
        today = now_local.strftime("%Y-%m-%d")
        if memory_store.get_meta("last_digest_date") == today:
            return

        digest_time = str(digest_cfg.get("time", DEFAULT_DIGEST_TIME))
        try:
            hour, minute = (int(p) for p in digest_time.split(":", 1))
        except ValueError:
            logger.error("Invalid digest.time %r in profile.yaml — expected HH:MM", digest_time)
            return

        scheduled = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_local < scheduled:
            return
        if now_local - scheduled > timedelta(hours=DIGEST_GRACE_HOURS):
            logger.info("Missed today's digest window (%s); skipping until tomorrow", digest_time)
            memory_store.set_meta("last_digest_date", today)
            return

        # Mark before running so a crashing digest can't fire in a loop
        memory_store.set_meta("last_digest_date", today)
        self._run_digest(today)

    def _run_digest(self, today: str) -> None:
        logger.info("Running morning digest for %s", today)
        try:
            dm = self.client.conversations_open(users=self.owner_user_id)
            channel = dm["channel"]["id"]
        except Exception:
            logger.exception("Could not open DM for digest")
            return

        prompt = DIGEST_PROMPT_PATH.read_text()
        poster = MessagePoster(self.client, channel)
        try:
            orchestrator.run(prompt, poster, conv_id=None, message_id=f"digest-{today}")
        except Exception:
            logger.exception("Digest run failed")
            poster.post("⚠️ I couldn't put together this morning's digest. Check the logs.")

    # ------------------------------------------------------------------
    # Maintenance: auto-archive + pending action expiry
    # ------------------------------------------------------------------

    def _run_maintenance(self, config: dict) -> None:
        expired = memory_store.expire_old_pending_actions(
            time.time() - PENDING_ACTION_TTL_HOURS * 3600
        )
        if expired:
            logger.info("Expired %d stale pending actions", expired)

        days = (config.get("memory") or {}).get(
            "auto_archive_days", DEFAULT_AUTO_ARCHIVE_DAYS
        )
        if not days:
            return

        cutoff = time.time() - days * 86400
        stale = memory_store.get_stale_active_conversations(cutoff)
        for conv_id in stale:
            try:
                gists = [m["gist"] for m in memory_store.get_message_gists(conv_id)]
                if gists:
                    gist = generate_conversation_gist(gists)
                else:
                    gist = "Empty thread — no exchanges recorded."
                memory_store.complete_conversation(conv_id, gist)
                logger.info("Auto-archived stale thread %s: %s", conv_id, gist[:80])
            except Exception:
                logger.exception("Failed to auto-archive thread %s", conv_id)


def _load_profile() -> dict:
    try:
        with open(PROFILE_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        logger.exception("Could not load profile.yaml; scheduler using defaults")
        return {}
