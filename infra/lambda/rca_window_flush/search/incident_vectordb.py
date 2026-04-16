"""
incident_vectordb.py - RCA Incident 向量索引，基于 S3 Vectors。

依赖 s3-vector-skill 工具链：
  - embed.py  — Bedrock Titan Embeddings v2，1024 维，带磁盘缓存
  - chunker.py — recursive / heading-aware 文本分块

S3 Vectors 配置：
  bucket: gp-incident-kb（环境变量 INCIDENT_VECTOR_BUCKET 可覆盖）
  index:  incidents-v1
  dimension: 1024（Titan v2）
  distanceMetric: cosine
"""
import logging
import os
import sys

import boto3

# 复用 s3-vector-skill 的 embedding 和分块实现
sys.path.insert(0, '/home/ubuntu/tech/s3-vector-skill/scripts')
from embed import embed_text  # noqa: E402
from chunker import chunk_text  # noqa: E402

logger = logging.getLogger(__name__)

REGION = os.environ.get('REGION', 'ap-northeast-1')
BUCKET = os.environ.get('INCIDENT_VECTOR_BUCKET', 'gp-incident-kb')
INDEX = 'incidents-v1'
VECTOR_DIMENSION = 1024
_BATCH_SIZE = 20


def _get_client():
    """返回 boto3 s3vectors 客户端。"""
    return boto3.client('s3vectors', region_name=REGION)


def ensure_bucket_and_index() -> None:
    """首次使用时创建向量桶和索引（幂等，已存在则跳过）。"""
    client = _get_client()

    try:
        client.get_vector_bucket(vectorBucketName=BUCKET)
    except client.exceptions.NotFoundException:
        client.create_vector_bucket(vectorBucketName=BUCKET)
        logger.info(f"Created vector bucket: {BUCKET}")

    try:
        client.get_index(vectorBucketName=BUCKET, indexName=INDEX)
    except client.exceptions.NotFoundException:
        client.create_index(
            vectorBucketName=BUCKET,
            indexName=INDEX,
            dataType='float32',
            dimension=VECTOR_DIMENSION,
            distanceMetric='cosine',
        )
        logger.info(f"Created index: {INDEX} in bucket {BUCKET}")


def index_incident(incident_id: str, report_text: str, metadata: dict) -> None:
    """将 RCA 报告分块向量化并写入 S3 Vectors。

    Args:
        incident_id: Incident 唯一标识（如 "inc-2026-04-01-abc123"）
        report_text: 完整的 RCA 报告文本
        metadata: 附加元数据，至少包含 severity、affected_service、root_cause、timestamp
    """
    ensure_bucket_and_index()

    chunks = chunk_text(report_text, chunk_size=512, chunk_overlap=64)
    client = _get_client()

    vectors = []
    for i, chunk in enumerate(chunks):
        vec = embed_text(chunk.content)
        vectors.append({
            'key': f"{incident_id}.chunk-{i:04d}",
            'data': {'float32': vec},
            'metadata': {
                'incident_id': incident_id,
                'severity': metadata.get('severity', ''),
                'affected_service': metadata.get('affected_service', ''),
                'root_cause': metadata.get('root_cause', ''),
                'content': chunk.content.encode('utf-8')[:1500].decode('utf-8', errors='ignore'),
                'timestamp': metadata.get('timestamp', ''),
            },
        })

    # 分批写入（S3 Vectors 每次 put 支持多个）
    for j in range(0, len(vectors), _BATCH_SIZE):
        client.put_vectors(
            vectorBucketName=BUCKET,
            indexName=INDEX,
            vectors=vectors[j:j + _BATCH_SIZE],
        )

    logger.info(f"Indexed {len(vectors)} chunks for incident {incident_id}")


def search_similar(query: str, top_k: int = 3, threshold: float = 0.6) -> list:
    """语义搜索与 query 最相似的历史 Incident。

    Args:
        query: 搜索查询文本（可以是故障描述、症状等）
        top_k: 返回最多几条结果
        threshold: 相似度阈值（0-1），低于此值的结果被过滤

    Returns:
        [{'incident_id':..., 'severity':..., 'affected_service':...,
          'root_cause':..., 'content':..., 'timestamp':..., 'score': float}]
    """
    vec = embed_text(query)
    client = _get_client()

    resp = client.query_vectors(
        vectorBucketName=BUCKET,
        indexName=INDEX,
        queryVector={'float32': vec},
        topK=top_k,
        returnDistance=True,
        returnMetadata=True,
    )

    results = []
    for r in resp.get('vectors', []):
        # cosine distance [0,2] → similarity [1, -1]，缩放到 [0,1]
        distance = r.get('distance', 0)
        score = round(1.0 - distance / 2.0, 4)
        if score >= threshold:
            entry = dict(r.get('metadata', {}))
            entry['score'] = score
            results.append(entry)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results
