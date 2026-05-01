"""DocumentSyncService -- keeps disk, profile dirs, and Qdrant in lockstep.

Single source of truth for the ``documentation_search`` Qdrant collection.

Responsibilities
----------------
- ``seed_shared_from_app(...)``  -- on boot, mirror bundled
  ``<repo>/documents/*.md`` into ``<OPENPA_WORKING_DIR>/documents/`` exactly:
  missing files are copied in, divergent files are overwritten, and files
  not present in the bundle are deleted. The bundle is authoritative for
  system documents, so any in-session edits or extras only live until the
  next restart.
- ``full_reconcile(scope, profile=None)`` -- scan a scope's on-disk directory,
  upsert new/changed docs, delete points whose source files have disappeared.
- ``apply_event(scope, path, event_type)`` -- handle a single watcher event
  (created / modified / deleted / moved-from / moved-to).

Thread safety: watchdog dispatches callbacks on its own thread, so all
methods that mutate the Qdrant collection take the same ``threading.Lock``.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

from qdrant_client.http.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
)

from app.documents.parser import parse_document
from app.lib.embedding import GrpcEmbeddings
from app.utils.logger import logger

if TYPE_CHECKING:
    from app.vectorstores.base import VectorStore

COLLECTION_NAME = "documentation_search"
SHARED_SCOPE = "shared"


class DocumentSyncService:
    """Sync `.md` documents under the working directory into Qdrant."""

    def __init__(
        self,
        *,
        working_dir: Path,
        vector_store: Optional["VectorStore"],
        embedding: GrpcEmbeddings,
    ):
        self._working_dir = Path(working_dir)
        self._vector_store = vector_store
        self._embedding = embedding
        self._lock = threading.Lock()
        self._collection_ready = False

    # ── Public paths ────────────────────────────────────────────────────────

    def shared_dir(self) -> Path:
        return self._working_dir / "documents"

    def profile_dir(self, profile: str) -> Path:
        return self._working_dir / profile / "documents"

    def scope_dir(self, scope: str) -> Path:
        return self.shared_dir() if scope == SHARED_SCOPE else self.profile_dir(scope)

    # ── System-level seeding ───────────────────────────────────────────────

    def seed_shared_from_app(self, app_documents_dir: Path) -> None:
        """Mirror bundled docs into the shared dir exactly.

        The bundle is authoritative for system documents. On every boot:

        - Files missing from dst are copied in.
        - Files whose content differs from the bundle are overwritten.
        - Any file in dst that doesn't exist in the bundle is deleted.

        Mid-session edits or extras therefore live only until the next
        restart. Profile-scoped docs under ``<working_dir>/<profile>/``
        are unaffected -- this method only touches the shared dir.
        """
        if not app_documents_dir.exists():
            return

        target = self.shared_dir()
        target.mkdir(parents=True, exist_ok=True)

        bundled_dst: set[Path] = set()
        copied = 0
        overwritten = 0

        for src in app_documents_dir.glob("**/*.md"):
            rel = src.relative_to(app_documents_dir)
            dst = target / rel
            bundled_dst.add(dst.resolve())

            if dst.exists():
                try:
                    same = _hash_file(src) == _hash_file(dst)
                except OSError as e:
                    logger.warning(f"[documents] failed to hash {dst}: {e}")
                    continue
                if same:
                    continue
                try:
                    shutil.copy2(src, dst)
                    overwritten += 1
                except OSError as e:
                    logger.warning(f"[documents] failed to overwrite {src} -> {dst}: {e}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except OSError as e:
                    logger.warning(f"[documents] failed to seed {src} -> {dst}: {e}")

        deleted = 0
        for path in list(target.rglob("*")):
            if path.is_dir():
                continue
            if path.resolve() in bundled_dst:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                logger.warning(f"[documents] failed to delete extra {path}: {e}")

        if copied or overwritten or deleted:
            logger.info(
                f"[documents] mirror from {app_documents_dir}: "
                f"copied={copied} overwritten={overwritten} deleted={deleted}"
            )

    # ── Reconciliation ─────────────────────────────────────────────────────

    def full_reconcile(self, scope: str) -> None:
        """Reconcile a single scope (``shared`` or a profile name) end-to-end.

        - Upsert every eligible `.md` whose content hash differs from Qdrant.
        - Delete points for files that are no longer on disk or have lost
          their frontmatter.
        """
        if self._vector_store is None:
            return

        directory = self.scope_dir(scope)
        directory.mkdir(parents=True, exist_ok=True)

        with self._lock:
            disk_state = self._scan_scope(scope)
            existing = self._fetch_scope_payloads(scope)

            disk_ids = {fid for fid, _ in disk_state.items()}
            existing_ids = {p["id"]: p for p in existing}

            to_delete = [pid for pid in existing_ids if pid not in disk_ids]
            if to_delete:
                self._delete_ids(to_delete)

            points: list[PointStruct] = []
            for fid, payload in disk_state.items():
                cur = existing_ids.get(fid)
                if cur and cur.get("content_hash") == payload["content_hash"]:
                    continue
                description = payload.pop("_description")
                vector = self._embed(description)
                if vector is None:
                    continue
                points.append(PointStruct(id=fid, vector=vector, payload=payload))

            if points:
                self._ensure_collection(len(points[0].vector))
                self._upsert_points(points)
                logger.info(
                    f"[documents] reconcile scope={scope!r}: upserted {len(points)} "
                    f"deleted {len(to_delete)}"
                )
            elif to_delete:
                logger.info(
                    f"[documents] reconcile scope={scope!r}: deleted {len(to_delete)}"
                )

    def apply_event(self, scope: str, path: Path) -> None:
        """Apply a single watcher event for ``path`` in ``scope``.

        Equivalent to a per-file reconcile: read disk, compute id+hash, then
        upsert or delete to match. Idempotent and tolerant of double-fired
        events.
        """
        if self._vector_store is None:
            return
        if path.suffix.lower() != ".md":
            return

        relpath = self._safe_relpath(scope, path)
        if relpath is None:
            return
        fid = self._file_id(scope, relpath)

        with self._lock:
            if not path.exists():
                self._delete_ids([fid])
                logger.debug(f"[documents] removed {scope}/{relpath} from vector store")
                return

            parsed = parse_document(path)
            if parsed is None:
                # File lost (or never had) valid frontmatter -- ensure it's gone.
                self._delete_ids([fid])
                logger.debug(
                    f"[documents] {scope}/{relpath} ineligible (no frontmatter "
                    "with description); skipped"
                )
                return

            content_hash = _hash_text(parsed.description + "\0" + parsed.body)
            existing = self._fetch_one(fid)
            if existing and existing.get("content_hash") == content_hash:
                return

            vector = self._embed(parsed.description)
            if vector is None:
                return
            self._ensure_collection(len(vector))
            payload = self._build_payload(
                scope=scope,
                relpath=relpath,
                file_path=str(path),
                description=parsed.description,
                content_hash=content_hash,
            )
            self._upsert_points([
                PointStruct(id=fid, vector=vector, payload=payload),
            ])
            logger.info(f"[documents] upserted {scope}/{relpath}")

    # ── Read helpers (used by the search tool) ─────────────────────────────

    def search(
        self,
        *,
        query: str,
        profile: str,
        limit: int = 10,
    ) -> list[dict]:
        """Vector-search the collection, filtered to ``shared`` + ``profile``.

        Returns Qdrant's payload dicts plus ``score``. Body content is loaded
        from disk by the caller, not stored here.
        """
        if self._vector_store is None:
            return []

        client = self._raw_client()
        if client is None:
            return []
        if not client.collection_exists(COLLECTION_NAME):
            return []

        try:
            vector = self._embedding.embed_query(query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embed_query failed: {e}")
            return []

        scope_filter = Filter(must=[
            FieldCondition(
                key="scope",
                match=MatchAny(any=[SHARED_SCOPE, profile]),
            ),
        ])
        try:
            response = client._client.query_points(
                collection_name=COLLECTION_NAME,
                query=vector,
                query_filter=scope_filter,
                limit=limit,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] query_points failed: {e}")
            return []

        results: list[dict] = []
        for point in response.points:
            payload = dict(point.payload or {})
            payload["score"] = point.score
            results.append(payload)
        return results

    @staticmethod
    def read_body(path: Path) -> Optional[str]:
        """Re-parse ``path`` and return only the body (frontmatter excluded).

        Returns None if the file is no longer eligible (e.g. it was deleted
        or had its frontmatter removed between search and read).
        """
        parsed = parse_document(path)
        return None if parsed is None else parsed.body

    # ── Internals: scanning and Qdrant plumbing ────────────────────────────

    def _scan_scope(self, scope: str) -> dict[int, dict]:
        """Build ``{file_id: payload-dict-with-_description}`` for a scope."""
        directory = self.scope_dir(scope)
        out: dict[int, dict] = {}
        if not directory.exists():
            return out

        for path in directory.glob("**/*.md"):
            parsed = parse_document(path)
            if parsed is None:
                continue
            relpath = self._safe_relpath(scope, path)
            if relpath is None:
                continue
            fid = self._file_id(scope, relpath)
            content_hash = _hash_text(parsed.description + "\0" + parsed.body)
            payload = self._build_payload(
                scope=scope,
                relpath=relpath,
                file_path=str(path),
                description=parsed.description,
                content_hash=content_hash,
            )
            payload["_description"] = parsed.description
            out[fid] = payload
        return out

    def _fetch_scope_payloads(self, scope: str) -> list[dict]:
        client = self._raw_client()
        if client is None or not client.collection_exists(COLLECTION_NAME):
            return []
        try:
            scope_filter = Filter(must=[
                FieldCondition(key="scope", match=MatchValue(value=scope)),
            ])
            offset = None
            rows: list[dict] = []
            while True:
                points, next_offset = client._client.scroll(
                    collection_name=COLLECTION_NAME,
                    scroll_filter=scope_filter,
                    limit=256,
                    offset=offset,
                    with_vectors=False,
                )
                for p in points:
                    payload = dict(p.payload or {})
                    payload["id"] = p.id
                    rows.append(payload)
                if next_offset is None:
                    break
                offset = next_offset
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] scroll failed for scope={scope}: {e}")
            return []

    def _fetch_one(self, fid: int) -> Optional[dict]:
        client = self._raw_client()
        if client is None or not client.collection_exists(COLLECTION_NAME):
            return None
        try:
            records = client._client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[fid],
                with_vectors=False,
            )
            if not records:
                return None
            return dict(records[0].payload or {})
        except Exception:  # noqa: BLE001
            return None

    def _ensure_collection(self, dimension: int) -> None:
        if self._collection_ready or self._vector_store is None:
            return
        client = self._raw_client()
        if client is None:
            return
        if not client.collection_exists(COLLECTION_NAME):
            client.create_named_collection(
                collection_name=COLLECTION_NAME, size=dimension,
            )
            logger.info(
                f"[documents] created Qdrant collection "
                f"{COLLECTION_NAME!r} (dim={dimension})"
            )
        self._collection_ready = True

    def _upsert_points(self, points: Iterable[PointStruct]) -> None:
        client = self._raw_client()
        if client is None:
            return
        client.add_points(collection_name=COLLECTION_NAME, points=list(points))

    def _delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        client = self._raw_client()
        if client is None or not client.collection_exists(COLLECTION_NAME):
            return
        try:
            client.delete_texts(collection_name=COLLECTION_NAME, ids=ids)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] delete_ids failed: {e}")

    def _embed(self, text: str) -> Optional[list[float]]:
        try:
            return self._embedding.embed_query(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[documents] embedding failed: {e}")
            return None

    def _raw_client(self):
        if self._vector_store is None:
            return None
        return getattr(self._vector_store, "_client", None)

    def _safe_relpath(self, scope: str, path: Path) -> Optional[str]:
        base = self.scope_dir(scope)
        try:
            return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
        except ValueError:
            return None

    @staticmethod
    def _file_id(scope: str, relpath: str) -> int:
        # Qdrant point ids must be unsigned 64-bit integers.
        digest = hashlib.blake2b(
            f"{scope}/{relpath}".encode("utf-8"), digest_size=8,
        ).digest()
        return int.from_bytes(digest, "big")

    def _build_payload(
        self,
        *,
        scope: str,
        relpath: str,
        file_path: str,
        description: str,
        content_hash: str,
    ) -> dict:
        return {
            "text": description,
            "scope": scope,
            "relpath": relpath,
            "file_path": file_path,
            "content_hash": content_hash,
            "name": Path(relpath).stem,
        }


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
