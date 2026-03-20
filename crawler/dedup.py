"""
Deduplication using SimHash on article titles.

SimHash cho phép so sánh 2 string bằng hamming distance trên bit-vector.
Distance <= threshold => coi là trùng nhau.
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass

import redis.asyncio as aioredis


DEDUP_KEY = "news:dedup:simhashes"
AI_DEDUP_KEY = "news:ai:dedup:simhashes"
DEDUP_TTL_SECONDS = 86400  # 24h


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, normalize unicode."""
    title = unicodedata.normalize("NFKC", title.lower())
    title = re.sub(r"[^\w\s]", "", title)
    return re.sub(r"\s+", " ", title).strip()


def _simhash(text: str, bits: int = 64) -> int:
    """
    Compute SimHash of text.

    Splits text into tokens, hashes each token, then aggregates
    bit-vectors weighted by sign → final hash as integer.
    """
    tokens = _normalize_title(text).split()
    if not tokens:
        return 0

    v = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1

    fingerprint = 0
    for i in range(bits):
        if v[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class DedupResult:
    is_duplicate: bool
    simhash: int
    matched_hash: int | None = None


# TODO: implement dedup check logic
# Đây là nơi bạn implement business logic quan trọng nhất:
# kiểm tra 1 article title có trùng với bất kỳ bài nào đã có trong Redis không.
#
# async def check_duplicate(redis: aioredis.Redis, title: str, threshold: int = 3) -> DedupResult:
#     """
#     Parameters:
#         redis: Redis connection
#         title: tiêu đề bài cần kiểm tra
#         threshold: hamming distance tối đa để coi là trùng (mặc định 3)
#
#     Returns DedupResult với:
#         - is_duplicate: True nếu tìm thấy bài trùng
#         - simhash: hash của title hiện tại
#         - matched_hash: hash của bài trùng (nếu có)
#
#     Hint: dùng _simhash() để tính hash, lấy tất cả stored hashes từ Redis
#     bằng SMEMBERS, rồi so sánh hamming distance từng cái.
#     Sau khi check xong, nếu KHÔNG trùng thì lưu hash vào Redis với SADD + EXPIRE.
#     """
#     pass


async def check_ai_duplicate(
    redis: aioredis.Redis, title: str, threshold: int = 6
) -> DedupResult:
    """
    Pre-AI semantic dedup: check if a similar story was already AI-processed.
    Uses a looser threshold than crawl dedup to catch same-story-different-source.
    Does NOT write to the set — call register_ai_simhash() after successful AI.
    """
    current_hash = _simhash(title)
    stored = await redis.smembers(AI_DEDUP_KEY)
    for raw in stored:
        stored_hash = int(raw)
        if _hamming_distance(current_hash, stored_hash) <= threshold:
            return DedupResult(is_duplicate=True, simhash=current_hash, matched_hash=stored_hash)
    return DedupResult(is_duplicate=False, simhash=current_hash)


async def register_ai_simhash(redis: aioredis.Redis, title: str) -> None:
    """Register that this title has been AI-processed (so future similar stories are skipped)."""
    h = _simhash(title)
    await redis.sadd(AI_DEDUP_KEY, h)
    await redis.expire(AI_DEDUP_KEY, DEDUP_TTL_SECONDS)


async def check_duplicate(redis: aioredis.Redis, title: str, threshold: int = 3) -> DedupResult:
    current_hash = _simhash(title)

    stored = await redis.smembers(DEDUP_KEY)
    for raw in stored:
        stored_hash = int(raw)
        if _hamming_distance(current_hash, stored_hash) <= threshold:
            return DedupResult(is_duplicate=True, simhash=current_hash, matched_hash=stored_hash)

    await redis.sadd(DEDUP_KEY, current_hash)
    await redis.expire(DEDUP_KEY, DEDUP_TTL_SECONDS)
    return DedupResult(is_duplicate=False, simhash=current_hash)
