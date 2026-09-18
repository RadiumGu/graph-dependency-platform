"""
embed.py - Bedrock Titan Embeddings v2 文本向量化。

原先该实现位于仓库外的 s3-vector-skill 工具链，被 search/incident_vectordb.py 通过
硬编码路径 sys.path.insert('/home/ubuntu/tech/s3-vector-skill/scripts') 引入。
该路径只存在于最初的开发机上，因此 Lambda 运行时必然 ImportError
（表现为日志中的 "No module named 'embed'"，语义检索与向量索引静默降级）。
此处将实现内联进仓库，去掉对机器本地路径的依赖。

契约（由 search/incident_vectordb.py 与 tests/test_05_unit_vectors.py 共同约束）：
  embed_text(text) -> list[float]，长度 = VECTOR_DIMENSION (1024)

线上资源（ap-northeast-1，已存在）：
  s3vectors 桶 gp-incident-kb / 索引 incidents-v1
  dataType=float32, dimension=1024, distanceMetric=cosine
维度必须与索引一致，否则 put_vectors 会被服务端拒绝。
"""
import hashlib
import json
import logging
import os
import threading

import boto3

logger = logging.getLogger(__name__)

# 必须与 s3vectors 索引 incidents-v1 的 dimension 一致
VECTOR_DIMENSION = 1024

MODEL_ID = os.environ.get('EMBED_MODEL_ID', 'amazon.titan-embed-text-v2:0')

# Titan v2 单次请求的输入上限约 8k token；按最坏情况（1 token ≈ 1 字符，CJK）留出余量。
# 超长文本由调用方先经 chunker 切分，这里只做兜底截断。
_MAX_INPUT_CHARS = int(os.environ.get('EMBED_MAX_INPUT_CHARS', '8000'))

# 磁盘缓存目录。Lambda 中只有 /tmp 可写，且同一容器的后续调用可复用。
_CACHE_DIR = os.environ.get('EMBED_CACHE_DIR', '/tmp/embed-cache')
_CACHE_ENABLED = os.environ.get('EMBED_CACHE_ENABLED', 'true').lower() != 'false'

_client = None
_client_lock = threading.Lock()


def _get_client():
    """惰性创建并复用 bedrock-runtime 客户端（避免每次调用重建连接）。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from shared import get_region
                _client = boto3.client('bedrock-runtime', region_name=get_region())
    return _client


def _cache_key(text: str) -> str:
    """按模型 + 维度 + 文本内容生成缓存键，换模型不会命中旧缓存。"""
    h = hashlib.sha256()
    h.update(MODEL_ID.encode('utf-8'))
    h.update(b'\x00')
    h.update(str(VECTOR_DIMENSION).encode('utf-8'))
    h.update(b'\x00')
    h.update(text.encode('utf-8'))
    return h.hexdigest()


def _cache_read(key: str):
    if not _CACHE_ENABLED:
        return None
    path = os.path.join(_CACHE_DIR, key + '.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            vec = json.load(f)
        if isinstance(vec, list) and len(vec) == VECTOR_DIMENSION:
            return vec
        # 维度不符说明缓存来自旧配置，视为未命中
        return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _cache_write(key: str, vec: list) -> None:
    if not _CACHE_ENABLED:
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = os.path.join(_CACHE_DIR, key + '.json')
        # 先写临时文件再 rename，避免并发下读到半截内容
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(vec, f)
        os.replace(tmp, path)
    except OSError as e:
        # 缓存是纯优化，写失败不能影响主流程
        logger.debug(f"embed cache write skipped: {e}")


def embed_text(text: str) -> list:
    """把文本转成 1024 维向量。

    Args:
        text: 待向量化的文本。空串或纯空白会抛 ValueError —— 写入零向量会污染
              cosine 索引的检索结果，静默返回更难排查。

    Returns:
        list[float]，长度为 VECTOR_DIMENSION。

    Raises:
        ValueError: 输入为空，或模型返回的维度与索引不一致。
    """
    if text is None or not text.strip():
        raise ValueError("embed_text: 输入文本为空")

    if len(text) > _MAX_INPUT_CHARS:
        logger.warning(
            f"embed_text: 输入 {len(text)} 字符超过上限 {_MAX_INPUT_CHARS}，已截断"
        )
        text = text[:_MAX_INPUT_CHARS]

    key = _cache_key(text)
    cached = _cache_read(key)
    if cached is not None:
        return cached

    body = json.dumps({
        'inputText': text,
        'dimensions': VECTOR_DIMENSION,
        'normalize': True,          # cosine 索引下归一化向量更稳定
    })

    resp = _get_client().invoke_model(
        modelId=MODEL_ID,
        contentType='application/json',
        accept='application/json',
        body=body,
    )
    payload = json.loads(resp['body'].read())
    vec = payload.get('embedding')

    if not isinstance(vec, list) or len(vec) != VECTOR_DIMENSION:
        raise ValueError(
            f"embed_text: 模型 {MODEL_ID} 返回维度 "
            f"{len(vec) if isinstance(vec, list) else type(vec).__name__}，"
            f"与索引要求的 {VECTOR_DIMENSION} 不一致"
        )

    _cache_write(key, vec)
    return vec
