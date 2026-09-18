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

import boto3

# embed / chunker 已内联进本仓库（rca/embed.py、rca/chunker.py）。
# 原先这里靠 sys.path.insert('/home/ubuntu/tech/s3-vector-skill/scripts') 引入，
# 该路径只存在于最初的开发机，导致 Lambda 上必然 "No module named 'embed'"，
# 语义检索与向量索引长期静默降级。
from embed import embed_text
from chunker import chunk_text

logger = logging.getLogger(__name__)

from shared import get_region
REGION = get_region()
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


def delete_incident_vectors(incident_id: str) -> int:
    """删除某个 Incident 的所有向量分块,返回删除条数。

    2026-08-28 新增。此前**只有写入路径没有删除路径**,后果实测如下:

      集成测试写 Incident 时会连带写向量。test_07/test_10 的清理只
      `DETACH DELETE` Neptune 节点,删不掉向量 —— 于是节点清干净了,
      向量却单调累积。一天下来索引从基线 18 条涨到 **56 条**,
      多出的 38 条全是内容高度相似的测试 Incident(都是 petsite)。

      search_similar 取 top_k,索引里塞满近乎相同的测试数据之后,
      刚写入的那条**排不进 top_k** —— 表现为"向量搜索找不到刚写的 Incident"。
      起初以为是最终一致性,实际是**索引污染**。

    这个不对称本身也是运维缺口:Incident 被从图谱删除后,它的向量仍会被
    检索到并注入 RCA 提示词,即"已删除的历史"继续影响判断。

    Args:
        incident_id: Incident 唯一标识

    Returns:
        实际删除的向量条数(找不到时返回 0,不抛异常)
    """
    client = _get_client()
    try:
        resp = client.list_vectors(vectorBucketName=BUCKET, indexName=INDEX)
    except Exception as e:
        logger.warning(f"list_vectors 失败，无法删除 {incident_id} 的向量: {e}")
        return 0

    prefix = f"{incident_id}.chunk-"
    keys = [v['key'] for v in resp.get('vectors', [])
            if v.get('key', '').startswith(prefix)]
    if not keys:
        return 0

    deleted = 0
    for j in range(0, len(keys), _BATCH_SIZE):
        batch = keys[j:j + _BATCH_SIZE]
        try:
            client.delete_vectors(
                vectorBucketName=BUCKET, indexName=INDEX, keys=batch,
            )
            deleted += len(batch)
        except Exception as e:
            logger.warning(f"删除向量失败 {batch[:3]}...: {e}")
    logger.info(f"Deleted {deleted} vector chunks for incident {incident_id}")
    return deleted


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
