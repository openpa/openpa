import grpc
from typing import List
from app.config.settings import BaseConfig
from app.proto import embedding_pb2, embedding_pb2_grpc
from grpc import RpcError


class GrpcEmbeddings():
    def __init__(self):
        channel_options = [
            ('grpc.keepalive_time_ms', 30000),
            ('grpc.keepalive_timeout_ms', 5000),
            ('grpc.http2.max_pings_without_data', 0),
            ('grpc.keepalive_permit_without_calls', 1),
        ]
        channel = grpc.insecure_channel(
            f"{BaseConfig.EMBEDDING_GRPC_HOST}:{BaseConfig.EMBEDDING_GRPC_PORT}",
            options=channel_options
        )
        self.stub = embedding_pb2_grpc.EmbeddingServiceStub(channel)
        self.channel = channel

    def create_batch(self, texts: List[str], bs: int = 32):
        return [texts[x: x + bs] for x in range(0, len(texts), bs)]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        batched_texts = self.create_batch(texts=texts)
        res = []
        for batch_texts in batched_texts:
            for text in batch_texts:
                try:
                    request = embedding_pb2.EmbeddingRequest(text=text)
                    response = self.stub.GetEmbeddings(request, timeout=30)
                    res.append(list(response.embeddings))
                except RpcError as e:
                    if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                        raise TimeoutError("gRPC handshake/timeout")
                    elif e.code() == grpc.StatusCode.UNAVAILABLE:
                        raise ConnectionError("gRPC connection reset")
                    raise
        return res

    def embed_query(self, text: str) -> List[float]:
        try:
            request = embedding_pb2.EmbeddingRequest(text=text)
            response = self.stub.GetEmbeddings(request, timeout=30)
            return list(response.embeddings)
        except RpcError as e:
            if e.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise TimeoutError("gRPC handshake/timeout")
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                raise ConnectionError("gRPC connection reset")
            raise
