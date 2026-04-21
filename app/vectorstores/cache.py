"""Shared helper for persisting EmbeddingTables in a vector store.

Used by `OpenPAAgent` (tool-card embeddings) and `gg_places` (place-type
embeddings) so both can skip gRPC calls on restart when cached vectors exist
and the cached key set still matches the current data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import pandas as pd

from app.lib.embedding import GrpcEmbeddings
from app.types import EmbeddingTable, ToolEmbeddingRecord
from app.utils.common import build_table_embeddings
from app.utils.logger import logger

if TYPE_CHECKING:
    from .base import VectorStore


def _normalize_records(
    data: "dict[str, ToolEmbeddingRecord] | dict[str, str]",
) -> dict[str, ToolEmbeddingRecord]:
    """Coerce a legacy ``dict[str, str]`` (id → text) into records.

    Callers like ``gg_places`` still pass plain text maps; wrap them so the
    persisted Qdrant payload has a uniform schema across every collection
    produced through this helper.
    """
    normalized: dict[str, ToolEmbeddingRecord] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "text" in value:
            normalized[key] = value  # type: ignore[assignment]
        else:
            normalized[key] = ToolEmbeddingRecord(
                text=str(value),
                tool_id=key,
                name=key,
                tool_type="place_type",
                enabled=True,
            )
    return normalized


def get_or_build_embedding_table(
    *,
    vector_store: Optional["VectorStore"],
    embedding: GrpcEmbeddings,
    data: "dict[str, ToolEmbeddingRecord] | dict[str, str]",
    collection_name: str,
) -> EmbeddingTable:
    """Return an EmbeddingTable, using the vector store as a cache.

    Flow:
    1. If data is empty, return an empty EmbeddingTable.
    2. Normalise legacy ``dict[str, str]`` callers into ``ToolEmbeddingRecord``
       form so the persisted payload carries filter metadata uniformly.
    3. If ``vector_store`` is provided, try to load from ``collection_name``:
       load succeeds only when the cached keys/text/tool_type/enabled exactly
       match the incoming records.
    4. Otherwise, generate via gRPC and persist for next time.
    """
    if not data:
        return EmbeddingTable(pd.DataFrame(columns=["id", "text", "embeddings"]))

    records = _normalize_records(data)

    if vector_store is not None:
        cached = _load_cached_table(vector_store, records, collection_name)
        if cached is not None:
            return cached

    table = build_table_embeddings(embedding, records)

    if vector_store is not None and not table.is_empty():
        _persist_table(vector_store, table, collection_name)

    return table


def _load_cached_table(
    vector_store: "VectorStore",
    data: dict[str, ToolEmbeddingRecord],
    collection_name: str,
) -> Optional[EmbeddingTable]:
    """Try to rebuild an EmbeddingTable from a cached Qdrant collection.

    Cache is treated as stale if identity, text, tool_type, or enabled differ
    from the incoming records. Legacy collections lacking ``tool_type`` in the
    payload fail this check → one-time rebuild on upgrade (desirable).
    """
    try:
        from .qdrant import QdrantClient

        client: QdrantClient = vector_store._client  # type: ignore[assignment]
        if not client.collection_exists(collection_name):
            logger.info(f"[vectorstore] Collection '{collection_name}' not found, will generate")
            return None

        points = client.scroll_all(collection_name=collection_name, with_vectors=True)
        if not points:
            return None

        stored_keys = {p.payload.get("key") for p in points}
        current_keys = set(data.keys())
        if stored_keys != current_keys:
            logger.info(
                f"[vectorstore] Data changed for '{collection_name}' "
                f"(cached: {len(stored_keys)}, current: {len(current_keys)}), regenerating"
            )
            return None

        stale_reason: Optional[str] = None
        for p in points:
            key = p.payload.get("key")
            rec = data.get(key)
            if rec is None:
                stale_reason = f"unknown key {key!r}"
                break
            if p.payload.get("text", "") != rec["text"]:
                stale_reason = f"text changed for {key!r}"
                break
            if p.payload.get("tool_type") != rec["tool_type"]:
                stale_reason = f"tool_type changed for {key!r}"
                break
            if bool(p.payload.get("enabled")) != bool(rec["enabled"]):
                stale_reason = f"enabled changed for {key!r}"
                break

        if stale_reason is not None:
            logger.info(
                f"[vectorstore] Cache stale for '{collection_name}' ({stale_reason}), regenerating"
            )
            return None

        rows = [
            {
                "id": p.payload.get("key"),
                "text": p.payload.get("text", ""),
                "embeddings": list(p.vector),
                "tool_id": p.payload.get("tool_id", p.payload.get("key")),
                "name": p.payload.get("name", p.payload.get("key")),
                "tool_type": p.payload.get("tool_type", ""),
                "enabled": bool(p.payload.get("enabled", True)),
            }
            for p in points
        ]
        logger.info(
            f"[vectorstore] Loaded {len(rows)} embeddings from '{collection_name}' (skipped gRPC)"
        )
        return EmbeddingTable(pd.DataFrame(rows))

    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vectorstore] Failed to load '{collection_name}': {e}")
        return None


def _persist_table(
    vector_store: "VectorStore",
    table: EmbeddingTable,
    collection_name: str,
) -> None:
    """Persist an EmbeddingTable to a Qdrant collection (recreates the collection)."""
    try:
        from qdrant_client.http.models import PointStruct

        from .qdrant import QdrantClient

        client: QdrantClient = vector_store._client  # type: ignore[assignment]
        df = table.dataframe
        if df.empty:
            return

        dimension = len(df.iloc[0]["embeddings"])
        client.create_named_collection(collection_name=collection_name, size=dimension)

        points = [
            PointStruct(
                id=int(i) + 1,
                vector=row["embeddings"],
                payload={
                    "key": row["id"],
                    "text": row["text"],
                    "tool_id": row.get("tool_id", row["id"]),
                    "name": row.get("name", row["id"]),
                    "tool_type": row.get("tool_type", ""),
                    "enabled": bool(row.get("enabled", True)),
                },
            )
            for i, row in df.iterrows()
        ]
        client.add_points(collection_name=collection_name, points=points)
        logger.info(f"[vectorstore] Persisted {len(points)} embeddings to '{collection_name}'")

    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vectorstore] Failed to persist '{collection_name}': {e}")
