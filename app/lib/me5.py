import grpc
from typing import List
from app.config.settings import BaseConfig
from app.proto import me5_embedding_pb2, me5_embedding_pb2_grpc
from app.types import VectorEmbeddingType
from grpc import RpcError


class Me5Embeddings():
    def __init__(self, embedding_type: VectorEmbeddingType):
        channel_options = [
            ('grpc.keepalive_time_ms', 30000),  # Keepalive ping mỗi 30s
            ('grpc.keepalive_timeout_ms', 5000),  # Timeout cho ping
            ('grpc.http2.max_pings_without_data', 0),  # Không limit pings
            ('grpc.keepalive_permit_without_calls', 1),  # Cho phép keepalive idle
        ]
        channel = grpc.insecure_channel(
            f"{BaseConfig.ME5_EMBEDDING_HOST}:{BaseConfig.ME5_EMBEDDING_PORT}",
            options=channel_options
        )
        stub = me5_embedding_pb2_grpc.ME5EmbeddingServiceStub(channel)
        self.stub = stub
        self.embedding_type = embedding_type
        self.channel = channel

    def create_batch(self, texts: List[str], bs: int = 32):
        return [texts[x: x + bs] for x in range(0, len(texts), bs)]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        batched_texts = self.create_batch(texts=texts)
        res = []
        for batch_texts in batched_texts:  # Fix: batched_texts là batch
            for text in batch_texts:  # Xử lý từng text trong batch
                try:
                    request = me5_embedding_pb2.EmbeddingRequest(text=text)
                    if self.embedding_type == VectorEmbeddingType.ME5_SMALL:
                        response = self.stub.GetSentenceEmbeddingsMe5Small(request, timeout=30)  # Thêm timeout 30s
                    else:
                        response = self.stub.GetSentenceEmbeddingsMe5Large(request, timeout=30)
                    # Convert protobuf RepeatedScalarContainer to Python list
                    res.append(list(response.sentence_embeddings))
                except RpcError as e:
                    if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                        raise TimeoutError("gRPC handshake/timeout")  # Để retry catch
                    elif e.code() == grpc.StatusCode.UNAVAILABLE:
                        raise ConnectionError("gRPC connection reset")  # Để retry
                    raise
        return res

    def embed_query(self, text: str) -> List[float]:
        try:
            request = me5_embedding_pb2.EmbeddingRequest(text=text)
            if self.embedding_type == VectorEmbeddingType.ME5_SMALL:
                response = self.stub.GetSentenceEmbeddingsMe5Small(request, timeout=30)  # Thêm timeout 30s
            else:
                response = self.stub.GetSentenceEmbeddingsMe5Large(request, timeout=30)
            # Convert protobuf RepeatedScalarContainer to Python list
            return list(response.sentence_embeddings)
        except RpcError as e:
            if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError("gRPC handshake/timeout")  # Để retry catch
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                raise ConnectionError("gRPC connection reset")  # Để retry
            raise
