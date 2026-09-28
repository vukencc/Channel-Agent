import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uuid
from ai_agent_startup import config
import chromadb
import os
from pathlib import Path
from typing import List, Dict, Any
from pydantic import dataclasses
import numpy as np

api_key = config.API_KEY
doc_dir = config.DOC_DIR

@dataclasses.dataclass
class DocLoader:
    dir: Path
        
    def load(self) -> List[Dict[str,Any]]:    
        docs = []
        file_exts = ['.txt','.md']
        
        for file_path in self.dir.iterdir():
            if file_path.is_file() and file_path.suffix in file_exts:
                with open(file_path,'r',encoding='utf-8') as f:
                    content = f.read()
                    
                docs.append({
                    'content': content,
                    'metadata': {
                        'source': str(file_path),
                        'filename': file_path.name
                    }
                })
        
        return docs
    
from sklearn.metrics.pairwise import cosine_similarity

from ai_agent_startup.rag.chunking import TextSplitter
from ai_agent_startup.rag.embedding import get_embedding_model

# 语义参数 → 实际数值。阈值按本地 bge-small-zh 在本语料上的实测分布校准：
# 相关查询 top1 约 0.55~0.64，不相关约 0.29~0.43，两者有重叠，所以阈值只是粗筛，
# 真正的判断交给模型（返回里带上命中数与分数）。
_STRICTNESS_THRESHOLD = {"strict": 0.55, "normal": 0.45, "loose": 0.30}
_BREADTH_TOP_K = {"narrow": 2, "normal": 4, "wide": 8}


def resolve_params(strictness: str = "normal", breadth: str = "normal") -> tuple[float, int]:
    """把语义参数映射成 (相似度阈值, 最大返回条数)。"""
    if strictness not in _STRICTNESS_THRESHOLD:
        raise ValueError(f"strictness 只能是 {list(_STRICTNESS_THRESHOLD)}，收到 {strictness!r}")
    if breadth not in _BREADTH_TOP_K:
        raise ValueError(f"breadth 只能是 {list(_BREADTH_TOP_K)}，收到 {breadth!r}")
    return _STRICTNESS_THRESHOLD[strictness], _BREADTH_TOP_K[breadth]


class SimpleVectorStore:
    """
    简化的向量存储（embedding 模型向量 + 余弦相似度）
    """
    def __init__(self):
        self.vectors = None
        self.documents = []
    
    def add_documents(self, docs: List[Dict[str, Any]]):
        """
        添加文档到向量存储
        """
        texts = [doc['content'] for doc in docs]
        self.documents = docs
        self.vectors = get_embedding_model().embed(texts)
    
    def search(self, query: str, top_k: int = 3, threshold: float | None = None) -> List[Dict]:
        """
        搜索相关文档；threshold 为相似度下限，低于它的结果会被丢弃（可能少于 top_k）。
        """
        if self.vectors is None or len(self.documents) == 0:
            return []
        
        query_vector = get_embedding_model().embed([query])
        similarities = cosine_similarity(query_vector, self.vectors)[0]
        
        results = []
        for idx in np.argsort(similarities)[::-1]:
            score = float(similarities[idx])
            if threshold is not None and score < threshold:
                break  # 已按分数降序，后面只会更低
            results.append({
                'document': self.documents[idx]['content'],
                'metadata': self.documents[idx]['metadata'],
                'score': score
            })
            if len(results) >= top_k:
                break
        
        return results

def rag_search(
    query: str,
    top_k: int | None = None,
    strictness: str = "normal",
    breadth: str = "normal",
) -> str:
    """
    从本地知识库检索与 query 最相关的文本片段，返回可直接阅读的字符串。

    strictness / breadth 是给模型用的语义参数，由 resolve_params 映射成阈值和条数；
    top_k 仅用于兼容旧的显式条数调用，通常不用传。
    """
    threshold, limit = resolve_params(strictness, breadth)
    if top_k is not None:
        limit = top_k

    docs = DocLoader(doc_dir).load() # type: ignore

    # 父子分块：父块=段落（保留上下文），子块=句子（用于匹配）
    splitter = TextSplitter()
    parents: Dict[str, Dict[str, Any]] = {}
    children: List[Dict[str, Any]] = []
    for doc in docs:
        filename = doc['metadata']['filename']
        for pi, (ptext, pstart, pend) in enumerate(splitter.split_parents(doc['content'])):
            parent_id = f"{filename}#p{pi}"
            parents[parent_id] = {
                'text': ptext,
                'metadata': {
                    **doc['metadata'],
                    'parent': pi,
                    'start': pstart,
                    'end': pend,
                },
            }
            for ci, (ctext, cstart, cend) in enumerate(splitter.split_children(ptext)):
                children.append({
                    'content': ctext,
                    'metadata': {
                        **doc['metadata'],
                        'parent_id': parent_id,
                        'child': ci,
                        'start': cstart,
                        'end': cend,
                    },
                })

    status = (
        f"[检索状态] strictness={strictness}（阈值 {threshold:.2f}）"
        f" / breadth={breadth}（最多 {limit} 条）"
    )

    if not children:
        return f"{status}\n命中 0/0 个段落：知识库为空，请检查 DOC_DIR 下是否有 .txt / .md 文件。"

    # 用子块匹配：子块小、主题单一，匹配更精准
    vector_store = SimpleVectorStore()
    vector_store.add_documents(children)
    hits = vector_store.search(query, len(children), threshold)

    # 子块 → 父块：按分数从高到低取，同一段落只返回一次
    results: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        parent_id = hit['metadata']['parent_id']
        if parent_id in seen:
            continue
        seen.add(parent_id)
        results.append({
            'document': parents[parent_id]['text'],
            'metadata': parents[parent_id]['metadata'],
            'score': hit['score'],
        })
        if len(results) >= limit:
            break

    # 命中为空不是错误，返回可判断的状态，交给模型决定是否放宽参数/改写 query 重试
    if not results:
        return (
            f"{status}\n"
            f"命中 0/{len(parents)} 个段落："
            f"{len(children)} 个子块中没有任何一个达到阈值 {threshold:.2f}。"
            "可放宽 strictness（strict → normal → loose），或改写 query 后重试。"
        )

    # 工具必须返回 str，模型才能直接阅读；这里整理成带来源和分数的文本
    body = "\n\n".join(
        f"[{i + 1}] 来源: {r['metadata']['filename']} (score={r['score']:.3f})\n{r['document']}"
        for i, r in enumerate(results)
    )
    return (
        f"{status}\n"
        f"命中 {len(results)}/{len(parents)} 个段落"
        f"（{len(children)} 个子块中命中 {len(hits)} 个，已按段落去重）：\n\n{body}"
    )