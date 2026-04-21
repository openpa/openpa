from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable
import uuid


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers.

    Any class implementing embed_documents and embed_query satisfies this
    protocol structurally (e.g. GrpcEmbeddings).
    """

    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...


class VectorStoreBase(ABC):
    """Interface for vector stores."""

    @abstractmethod
    def create_collection(
        self,
        **kwargs: Any,
    ) -> str:
        """Create a collection."""

    @abstractmethod
    def list_collections(self) -> List[str]:
        """Get collections."""

    @abstractmethod
    def delete_collection(
        self,
        collection_name: str,
        force: bool = False,
        **kwargs: Any,
    ) -> None:
        """Delete a collection."""

    @abstractmethod
    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Add texts to a collection."""

    @abstractmethod
    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Get texts from a collection."""

    @abstractmethod
    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Update texts in a collection."""

    @abstractmethod
    def delete_texts(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Delete texts from a collection."""

    @abstractmethod
    def query(
        self,
        query_text: str,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        limit: int = 3,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Query text from a collection."""

    def create_collection_uuid(self) -> str:
        """Create a uuid."""
        return str(uuid.uuid4()).replace("-", "")


class VectorStore(VectorStoreBase):
    def __init__(
        self,
        client: VectorStoreBase,
    ):
        self._client = client

    def create_collection(
        self,
        **kwargs: Any,
    ) -> str:
        """Create a collection."""
        return self._client.create_collection(**kwargs)

    def list_collections(
        self,
    ) -> List[str]:
        """List collections."""
        return self._client.list_collections()

    def delete_collection(
        self,
        collection_name: str,
        force: bool = False,
        **kwargs: Any,
    ) -> None:
        """Delete a collection."""
        return self._client.delete_collection(collection_name, force=force, **kwargs)

    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Add texts to a collection."""
        return self._client.add_texts(
            collection_name=collection_name,
            embedding_function=embedding_function,
            texts=texts,
            ids=ids,
            metadatas=metadatas,
            **kwargs,
        )

    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Get texts from a collection."""
        return self._client.get_texts(
            collection_name=collection_name,
            filter=filter,
            ids=ids,
            **kwargs,
        )

    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Update texts in a collection."""
        return self._client.update_texts(
            collection_name=collection_name,
            embedding_function=embedding_function,
            texts=texts,
            ids=ids,
            metadatas=metadatas,
            **kwargs,
        )

    def delete_texts(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Delete texts from a collection."""
        self._client.delete_texts(
            collection_name=collection_name,
            filter=filter,
            ids=ids,
            **kwargs,
        )

    def query(
        self,
        query_text: str,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        limit: int = 3,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Query text from a collection."""
        return self._client.query(
            query_text=query_text,
            collection_name=collection_name,
            embedding_function=embedding_function,
            limit=limit,
            filter=filter,
            **kwargs,
        )
