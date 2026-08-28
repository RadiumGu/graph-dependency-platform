"""
chunker.py - 文本分块（heading-aware + recursive 回退）。

与 embed.py 同因缺失：原实现在仓库外的 s3-vector-skill 工具链里，
靠硬编码的 /home/ubuntu/... 路径引入，Lambda 中必然 ImportError。

契约（由 search/incident_vectordb.py 与 tests/test_05_unit_vectors.py 共同约束）：
  chunk_text(text, chunk_size=512, chunk_overlap=64) -> list[Chunk]
  Chunk 必须暴露 .content (str) 与 .tokens (int)
  测试断言：每个 chunk 的 tokens <= chunk_size + chunk_overlap

token 计数说明：RCA 报告是中英混排，这里用"英文按空白词、CJK 按字"的估算，
不引入 tiktoken 之类的额外依赖（Lambda 包体积与 arm64 轮子可用性都是约束）。
估算偏保守（倾向高估），保证不会超出 Titan 的真实上限。
"""
import re
from dataclasses import dataclass, field
from typing import List

# 递归切分的分隔符，从语义最强到最弱依次尝试
_SEPARATORS = [
    '\n\n',    # 段落
    '\n',      # 换行
    '。',      # 中文句号
    '！',
    '？',
    '. ',      # 英文句末
    '；',
    '; ',
    '，',
    ', ',
    ' ',       # 词
    '',        # 兜底：按字符硬切
]

# markdown 标题行
_HEADING_RE = re.compile(r'^(#{1,6})\s+\S', re.MULTILINE)

# CJK 统一表意文字 + 常用标点
_CJK_RE = re.compile(r'[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]')


def count_tokens(text: str) -> int:
    """估算 token 数。

    CJK 字符按 1 token/字，其余按空白分词后每词 ~1.3 token（BPE 经验值）。
    偏高估，宁可多切一刀也不要超模型上限。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub(' ', text)
    words = len(non_cjk.split())
    return cjk + int(words * 1.3 + 0.5)


@dataclass
class Chunk:
    """一个文本块。

    content: 块内容
    tokens:  估算 token 数
    index:   在原文中的序号
    heading: 该块所属的最近标题（heading-aware 切分时填充，便于检索时展示上下文）
    """
    content: str
    tokens: int
    index: int = 0
    heading: str = ''
    metadata: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.content)


def _split_recursive(text: str, chunk_size: int, seps: List[str]) -> List[str]:
    """按分隔符优先级递归切分，直到每段的 token 估算不超过 chunk_size。"""
    if count_tokens(text) <= chunk_size:
        return [text] if text.strip() else []

    if not seps:
        return [text] if text.strip() else []

    sep = seps[0]
    rest = seps[1:]

    if sep == '':
        # 兜底：按字符硬切。用 token/字符 比例估算每片字符数，至少 1。
        tokens = count_tokens(text)
        ratio = max(len(text) / tokens, 1.0) if tokens else 1.0
        step = max(int(chunk_size * ratio), 1)
        return [text[i:i + step] for i in range(0, len(text), step)]

    parts = text.split(sep)
    # 保留分隔符，避免还原后语义粘连
    pieces = [p + sep for p in parts[:-1]] + [parts[-1]]

    out: List[str] = []
    buf = ''
    for p in pieces:
        cand = buf + p
        if count_tokens(cand) <= chunk_size:
            buf = cand
            continue
        if buf:
            out.append(buf)
            buf = ''
        # 单个 piece 自身就超限 → 用更弱的分隔符继续拆
        if count_tokens(p) > chunk_size:
            out.extend(_split_recursive(p, chunk_size, rest))
        else:
            buf = p
    if buf.strip():
        out.append(buf)
    return [c for c in out if c.strip()]


def _apply_overlap(pieces: List[str], chunk_overlap: int) -> List[str]:
    """在相邻块之间加入重叠尾部，避免边界处语义被切断。

    重叠量按 token 估算换算成字符数取自上一块的尾部。
    """
    if chunk_overlap <= 0 or len(pieces) <= 1:
        return pieces

    out = [pieces[0]]
    for prev, cur in zip(pieces, pieces[1:]):
        tokens = count_tokens(prev)
        ratio = max(len(prev) / tokens, 1.0) if tokens else 1.0
        tail_chars = min(int(chunk_overlap * ratio), len(prev))
        out.append(prev[len(prev) - tail_chars:] + cur if tail_chars else cur)
    return out


def chunk_text(text: str, chunk_size: int = 512, chunk_overlap: int = 64) -> List[Chunk]:
    """把文本切分成不超过 chunk_size token 的块，相邻块带 chunk_overlap 重叠。

    有 markdown 标题时先按标题分段（heading-aware），段内再递归切分，
    这样一个块不会横跨两个不相关的章节。

    Args:
        text: 原文
        chunk_size: 每块 token 上限
        chunk_overlap: 相邻块的重叠 token 数

    Returns:
        list[Chunk]；空输入返回空列表。
    """
    if not text or not text.strip():
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_text: chunk_size 必须为正")
    if chunk_overlap < 0:
        raise ValueError("chunk_text: chunk_overlap 不能为负")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_text: chunk_overlap 必须小于 chunk_size")

    # heading-aware：按标题把原文切成若干 (heading, body) 段
    matches = list(_HEADING_RE.finditer(text))
    sections = []
    if matches:
        if matches[0].start() > 0:
            sections.append(('', text[:matches[0].start()]))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            line_end = text.find('\n', m.start())
            heading = text[m.start():line_end if line_end != -1 else end].strip()
            sections.append((heading, text[m.start():end]))
    else:
        sections.append(('', text))

    chunks: List[Chunk] = []
    for heading, body in sections:
        pieces = _split_recursive(body, chunk_size, _SEPARATORS)
        pieces = _apply_overlap(pieces, chunk_overlap)
        for p in pieces:
            content = p.strip()
            if not content:
                continue
            chunks.append(Chunk(
                content=content,
                tokens=count_tokens(content),
                index=len(chunks),
                heading=heading,
            ))

    return chunks
