"""
WebSocket Event Manager for real-time workflow visualization.

Broadcasts events for:
- Crawl operations (fetch, parse, dedup)
- AI processing (rewrite, synthesis)
- Distribution (webhook, telegram)
"""

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import WebSocket


class WebSocketManager:
    """Manages WebSocket connections and broadcasts workflow events."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all connected clients.

        Args:
            event_type: Type of event (crawl, ai, webhook, etc.)
            data: Event payload
        """
        message = {
            "type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data,
        }

        # Remove disconnected clients
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    # Crawl Events
    async def emit_crawl_start(
        self, source_id: str, source_name: str, url: str
    ) -> None:
        """Emit event when crawl starts for a source."""
        await self.broadcast(
            "crawl.start",
            {
                "source_id": source_id,
                "source_name": source_name,
                "url": url,
            },
        )

    async def emit_crawl_success(
        self,
        source_id: str,
        source_name: str,
        articles_found: int,
        duplicates: int,
        duration_ms: int,
    ) -> None:
        """Emit event when crawl succeeds."""
        await self.broadcast(
            "crawl.success",
            {
                "source_id": source_id,
                "source_name": source_name,
                "articles_found": articles_found,
                "duplicates": duplicates,
                "duration_ms": duration_ms,
            },
        )

    async def emit_crawl_error(
        self, source_id: str, source_name: str, error: str
    ) -> None:
        """Emit event when crawl fails."""
        await self.broadcast(
            "crawl.error",
            {
                "source_id": source_id,
                "source_name": source_name,
                "error": error,
            },
        )

    # Article Events
    async def emit_article_saved(
        self,
        article_id: str,
        title: str,
        source_id: str,
        category: str,
        is_duplicate: bool,
    ) -> None:
        """Emit event when article is saved to Redis."""
        await self.broadcast(
            "article.saved",
            {
                "article_id": article_id,
                "title": title,
                "source_id": source_id,
                "category": category,
                "is_duplicate": is_duplicate,
            },
        )

    # AI Events
    async def emit_ai_start(self, article_id: str, title: str) -> None:
        """Emit event when AI processing starts."""
        await self.broadcast(
            "ai.start",
            {
                "article_id": article_id,
                "title": title,
            },
        )

    async def emit_ai_success(
        self, article_id: str, title: str, duration_ms: int
    ) -> None:
        """Emit event when AI rewrite succeeds."""
        await self.broadcast(
            "ai.success",
            {
                "article_id": article_id,
                "title": title,
                "duration_ms": duration_ms,
            },
        )

    async def emit_ai_error(self, article_id: str, title: str, error: str) -> None:
        """Emit event when AI processing fails."""
        await self.broadcast(
            "ai.error",
            {
                "article_id": article_id,
                "title": title,
                "error": error,
            },
        )

    # Topic Synthesis Events
    async def emit_synthesis_start(self, category: str, article_count: int) -> None:
        """Emit event when topic synthesis starts."""
        await self.broadcast(
            "synthesis.start",
            {
                "category": category,
                "article_count": article_count,
            },
        )

    async def emit_synthesis_success(
        self, category: str, synthetics_generated: int, duration_ms: int
    ) -> None:
        """Emit event when synthesis succeeds."""
        await self.broadcast(
            "synthesis.success",
            {
                "category": category,
                "synthetics_generated": synthetics_generated,
                "duration_ms": duration_ms,
            },
        )

    # Webhook Events
    async def emit_webhook_start(
        self, article_id: str, title: str, webhook_name: str
    ) -> None:
        """Emit event when webhook dispatch starts."""
        await self.broadcast(
            "webhook.start",
            {
                "article_id": article_id,
                "title": title,
                "webhook_name": webhook_name,
            },
        )

    async def emit_webhook_success(
        self,
        article_id: str,
        title: str,
        webhook_name: str,
        status_code: int,
        duration_ms: int,
    ) -> None:
        """Emit event when webhook succeeds."""
        await self.broadcast(
            "webhook.success",
            {
                "article_id": article_id,
                "title": title,
                "webhook_name": webhook_name,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )

    async def emit_webhook_error(
        self, article_id: str, title: str, webhook_name: str, error: str
    ) -> None:
        """Emit event when webhook fails."""
        await self.broadcast(
            "webhook.error",
            {
                "article_id": article_id,
                "title": title,
                "webhook_name": webhook_name,
                "error": error,
            },
        )

    # Telegram Events
    async def emit_telegram_start(
        self, article_id: str, title: str, channel_name: str
    ) -> None:
        """Emit event when Telegram dispatch starts."""
        await self.broadcast(
            "telegram.start",
            {
                "article_id": article_id,
                "title": title,
                "channel_name": channel_name,
            },
        )

    async def emit_telegram_success(
        self, article_id: str, title: str, channel_name: str, duration_ms: int
    ) -> None:
        """Emit event when Telegram succeeds."""
        await self.broadcast(
            "telegram.success",
            {
                "article_id": article_id,
                "title": title,
                "channel_name": channel_name,
                "duration_ms": duration_ms,
            },
        )

    async def emit_telegram_error(
        self, article_id: str, title: str, channel_name: str, error: str
    ) -> None:
        """Emit event when Telegram fails."""
        await self.broadcast(
            "telegram.error",
            {
                "article_id": article_id,
                "title": title,
                "channel_name": channel_name,
                "error": error,
            },
        )


# Global singleton instance
ws_manager = WebSocketManager()
