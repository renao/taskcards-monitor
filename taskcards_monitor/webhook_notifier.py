"""Webhook notification module for TaskCards monitor.

Sends a compact text message to a webhook endpoint (e.g. a Home Assistant
webhook automation) whenever board changes are detected. The payload is JSON
so that the receiving automation can either use the pre-rendered ``message``
field directly or build its own text from the structured fields.
"""

import httpx

from .changes import ChangeSet


class WebhookNotifier:
    """Send webhook notifications about board changes."""

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
    ) -> bool:
        """Send a webhook notification if there are changes (not on first run).

        Args:
            board_id: The board identifier
            board_name: The board name (optional)
            timestamp: Timestamp of the check
            changes: ChangeSet from BoardMonitor.detect_changes()
            token: View token for private boards (optional)

        Returns:
            True if a notification was sent, False otherwise
        """
        # Don't notify on first run (baseline) or when nothing changed
        if changes.is_first_run or not changes.has_changes():
            return False

        board_url = f"https://www.taskcards.de/#/board/{board_id}/view"
        if token:
            board_url += f"?token={token}"

        message = self._build_message(board_name or board_id, board_url, changes)

        payload = {
            "message": message,
            "board_id": board_id,
            "board_name": board_name or board_id,
            "board_url": board_url,
            "timestamp": timestamp,
            "added_count": len(changes.cards_added),
            "removed_count": len(changes.cards_removed),
            "changed_count": len(changes.cards_modified),
        }

        response = httpx.post(self.webhook_url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        return True

    @staticmethod
    def _build_message(board_name: str, board_url: str, changes: ChangeSet) -> str:
        """Build a compact human-readable message describing the changes."""
        added = len(changes.cards_added)
        removed = len(changes.cards_removed)
        modified = len(changes.cards_modified)

        summary_parts = []
        if added:
            summary_parts.append(f"{added} neu")
        if modified:
            summary_parts.append(f"{modified} geändert")
        if removed:
            summary_parts.append(f"{removed} entfernt")
        summary = ", ".join(summary_parts) if summary_parts else "Änderungen"

        lines = [f"📋 TaskCards-Update: {board_name} ({summary})", ""]

        for card in changes.cards_added:
            column = f" [{card.column}]" if card.column else ""
            lines.append(f"➕ {card.title or '(ohne Titel)'}{column}")

        for card in changes.cards_modified:
            title = card.new_title or card.old_title or "(ohne Titel)"
            details = []
            if card.old_title != card.new_title:
                details.append("Titel")
            if card.old_description != card.new_description:
                details.append("Beschreibung")
            if card.old_link != card.new_link:
                details.append("Link")
            if card.old_column != card.new_column:
                details.append(f"Spalte: {card.old_column} → {card.new_column}")
            if card.attachments_added or card.attachments_removed:
                details.append("Anhänge")
            detail_str = f" ({', '.join(details)})" if details else ""
            lines.append(f"✏️ {title}{detail_str}")

        for card in changes.cards_removed:
            lines.append(f"➖ {card.title or '(ohne Titel)'}")

        lines.append("")
        lines.append(board_url)

        return "\n".join(lines)
