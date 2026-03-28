"""
News Lake — Cloudflare R2 cold storage.

Writes full enriched articles as JSONL objects using Hive-style partition keys.
Articles are written once enrichment is complete (entities, sentiment, topic_id).

Query offline with DuckDB:
  SELECT * FROM read_json_auto('s3://news-raw-articles/articles/dt=2026-03-28/**/*.jsonl')

R2 credentials go in config/settings.yaml under:
  lake:
    enabled: true
    r2:
      account_id: "<CF_ACCOUNT_ID>"
      access_key_id: "<R2_ACCESS_KEY>"
      secret_access_key: "<R2_SECRET_KEY>"
      bucket: "news-raw-articles"
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_store: Optional["R2LakeStore"] = None


class R2LakeStore:
    def __init__(
        self,
        account_id: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    def build_partition_key(self, article: dict) -> str:
        """
        Build the R2 object key for a fully enriched article.

        Available fields on `article`:
          id, source_id, source_name, url, title, category, lang,
          published_at (ISO str), fetched_at (ISO str),
          type ("original" | "synthetic"),
          entities (JSON list), sentiment, topic_id,
          ai_status, ai_enrich_status

        Target layout (Hive-style, DuckDB/Athena compatible):
          articles/dt=YYYY-MM-DD/cat={category}/src={source_id}/{article_id}.jsonl
          synthetic/dt=YYYY-MM-DD/cat={category}/{article_id}.jsonl

        Design questions to consider:
          • Should `dt` use published_at (article time) or fetched_at (ingest time)?
          • Should synthetic articles go under a separate top-level prefix?
          • Is `src` partition worth it? (39 sources × N days = many small files)

        Return empty string ("") to skip archiving this article.
        """
        # TODO: implement your partition key strategy (5–10 lines)
        # Example skeleton:
        #
        #   dt = article.get("published_at", "")[:10]   # "2026-03-28"
        #   category = article.get("category", "unknown")
        #   source_id = article.get("source_id", "unknown")
        #   article_id = article.get("id", "unknown")
        #   article_type = article.get("type", "original")
        #
        #   if article_type == "synthetic":
        #       return f"synthetic/dt={dt}/cat={category}/{article_id}.jsonl"
        #   return f"articles/dt={dt}/cat={category}/src={source_id}/{article_id}.jsonl"

        dt = (article.get("published_at") or article.get("fetched_at") or "")[:10]
        if not dt:
            dt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        category = article.get("category", "unknown").lower().replace(" ", "_")
        source_id = article.get("source_id", "unknown")
        article_id = article.get("id", "unknown")
        article_type = article.get("type", "original")

        if article_type == "synthetic":
            return f"synthetic/dt={dt}/cat={category}/{article_id}.jsonl"
        return f"articles/dt={dt}/cat={category}/src={source_id}/{article_id}.jsonl"

    # ------------------------------------------------------------------
    # Internal sync upload (runs in thread executor — R2/S3 calls are blocking)
    # ------------------------------------------------------------------

    def _put_object(self, key: str, body: bytes) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
        )

    def _object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def archive_article(self, article: dict) -> bool:
        """
        Write a fully enriched article as a single JSONL line to R2.
        Skips if the object key already exists (idempotent on re-runs).
        Returns True if written, False if skipped or failed.
        """
        try:
            key = self.build_partition_key(article)
        except NotImplementedError:
            logger.debug("[lake] build_partition_key not implemented — skipping")
            return False

        if not key:
            return False

        try:
            exists = await asyncio.to_thread(self._object_exists, key)
            if exists:
                logger.debug(f"[lake] already archived: {key}")
                return False

            payload = json.dumps(article, ensure_ascii=False, default=str) + "\n"
            await asyncio.to_thread(self._put_object, key, payload.encode("utf-8"))
            logger.debug(f"[lake] archived → {key}")
            return True
        except Exception as exc:
            logger.warning(f"[lake] archive failed for {article.get('id')}: {exc}")
            return False

    async def archive_batch(self, articles: list[dict]) -> tuple[int, int]:
        """
        Archive a list of articles concurrently (max 5 parallel uploads).
        Returns (written, skipped_or_failed).
        """
        sem = asyncio.Semaphore(5)

        async def _bounded(art: dict) -> bool:
            async with sem:
                return await self.archive_article(art)

        results = await asyncio.gather(*[_bounded(a) for a in articles])
        written = sum(1 for r in results if r)
        return written, len(articles) - written


# ---------------------------------------------------------------------------
# Module-level singleton — init once from main.py lifespan
# ---------------------------------------------------------------------------

def init_lake(cfg: dict) -> None:
    """Initialise the R2 lake store from the `lake` config section."""
    global _store
    r2 = cfg.get("r2", {})
    account_id = r2.get("account_id", "")
    access_key = r2.get("access_key_id", "")
    secret_key = r2.get("secret_access_key", "")
    bucket = r2.get("bucket", "news-raw-articles")

    if not all([account_id, access_key, secret_key]):
        logger.warning("[lake] R2 credentials incomplete — lake disabled")
        return

    _store = R2LakeStore(
        account_id=account_id,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
    )
    logger.info(f"[lake] R2 lake store ready → bucket={bucket}")


def get_lake() -> Optional[R2LakeStore]:
    return _store
