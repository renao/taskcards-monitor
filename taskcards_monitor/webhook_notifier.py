"""Webhook notification module for TaskCards monitor.

Sends one message per changed card to a webhook endpoint (e.g. a Home Assistant
webhook automation) whenever board changes are detected. Each message contains
the card title, its last-modified time (de-DE / Europe/Berlin), the description
as plain text, a hint about attachments and a link to open the board.
"""

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .changes import ChangeSet


def _berlin_offset_hours(dt_utc: datetime) -> int:
    """Return the Europe/Berlin UTC offset (1 = MEZ, 2 = MESZ) without tzdata."""
    year = dt_utc.year

    def last_sunday_0100(month: int) -> datetime:
        d = datetime(year, month, 31, 1, 0, tzinfo=timezone.utc)
        while d.weekday() != 6:  # 6 = Sunday
            d -= timedelta(days=1)
        return d

    return 2 if last_sunday_0100(3) <= dt_utc < last_sunday_0100(10) else 1


def _format_modified(value: Any) -> str | None:
    """Format a taskcards 'modified' value (epoch ms) as de-DE Berlin time."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    dt_utc = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    local = dt_utc + timedelta(hours=_berlin_offset_hours(dt_utc))
    return local.strftime("%d.%m.%Y, %H:%M Uhr")


def _clean_description(desc: str | None) -> str:
    """Convert a taskcards HTML description into readable plain text."""
    if not desc:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", desc)
    text = re.sub(r"(?i)</(div|p|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _attachment_hint(attachments: list) -> str | None:
    """Return a compact hint like '📎 3 Bilder – im Board ansehen'."""
    if not attachments:
        return None
    n = len(attachments)

    def _mime(a: Any) -> str:
        if isinstance(a, dict):
            return a.get("mimetype") or ""
        return getattr(a, "mime_type", "") or ""

    n_images = sum(1 for a in attachments if _mime(a).startswith("image/"))
    if n_images == n:
        label = "Bild" if n == 1 else "Bilder"
    else:
        label = "Anhang" if n == 1 else "Anhänge"
    return f"📎 {n} {label} – im Board ansehen"


class WebhookNotifier:
    """Send one webhook notification per changed card."""

    def __init__(self, webhook_url: str, timeout: int = 30):
        """Initialize the webhook notifier.

        Args:
            webhook_url: The webhook URL to POST notifications to
            timeout: HTTP request timeout in seconds
        """
        if not webhook_url:
            raise ValueError("Webhook URL is required")
        self.webhook_url = webhook_url
        self.timeout = timeout

    def notify_changes(
        self,
        board_id: str,
        board_name: str | None,
        timestamp: str,
        changes: ChangeSet,
        token: str | None = None,
        board_state=None,
    ) -> bool:
        """Send one webhook message per changed card (not on first run).

        Args:
            board_id: The board identifier
            board_name: The board name (optional)
            timestamp: Timestamp of the check
            changes: ChangeSet from BoardMonitor.detect_changes()
            token: View token for private boards (optional)
            board_state: Current BoardState, used to look up each card's
                last-modified time and attachments (optional)

        Returns:
            True if at least one notification was sent, False otherwise
        """
        if changes.is_first_run or not changes.has_changes():
            return False

        board_url = f"https://www.taskcards.de/#/board/{board_id}/view"
        if token:
            board_url += f"?token={token}"
        name = board_name or board_id

        def _card(card_id: str) -> dict:
            if board_state is not None:
                return board_state.get_card(card_id) or {}
            return {}

        sent = 0

        for card in changes.cards_added:
            full = _card(card.id)
            message = self._card_message(
                icon="🆕",
                board_name=name,
                title=card.title,
                modified=_format_modified(full.get("modified")),
                description=card.description,
                attachments=full.get("attachments") or card.attachments,
                board_url=board_url,
            )
            self._post(message, board_id, name, board_url, timestamp, card.id, "added")
            sent += 1

        for card in changes.cards_modified:
            full = _card(card.id)
            message = self._card_message(
                icon="✏️",
                board_name=name,
                title=card.new_title or card.old_title,
                modified=_format_modified(full.get("modified")),
                description=card.new_description,
                attachments=full.get("attachments") or [],
                board_url=board_url,
            )
            self._post(message, board_id, name, board_url, timestamp, card.id, "modified")
            sent += 1

        for card in changes.cards_removed:
            title = card.title or "(ohne Titel)"
            message = "\n".join(
                [
                    f"🗑️ {name}: Karte entfernt – {title}",
                    "",
                    f"🔗 Board öffnen: {board_url}",
                ]
            )
            self._post(message, board_id, name, board_url, timestamp, card.id, "removed")
            sent += 1

        return sent > 0

    @staticmethod
    def _card_message(
        icon: str,
        board_name: str,
        title: str | None,
        modified: str | None,
        description: str | None,
        attachments: list,
        board_url: str,
    ) -> str:
        """Build a single detailed card message."""
        parts = [f"{icon} {board_name}: {title or '(ohne Titel)'}"]
        if modified:
            parts.append(f"🕒 {modified}")
        desc = _clean_description(description)
        if desc:
            parts += ["", desc]
        hint = _attachment_hint(attachments or [])
        if hint:
            parts += ["", hint]
        parts += ["", f"🔗 Board öffnen: {board_url}"]
        return "\n".join(parts)

    def _post(
        self,
        message: str,
        board_id: str,
        board_name: str,
        board_url: str,
        timestamp: str,
        card_id: str,
        change_type: str,
    ) -> None:
        payload = {
            "message": message,
            "board_id": board_id,
            "board_name": board_name,
            "board_url": board_url,
            "timestamp": timestamp,
            "card_id": card_id,
            "change_type": change_type,
        }
        response = httpx.post(self.webhook_url, json=payload, timeout=self.timeout)
        response.raise_for_status()
