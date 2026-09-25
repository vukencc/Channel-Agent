import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uuid
import config
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
    
@dataclasses.dataclass
class TextSplitter:
    chunk_size: int = 100
    chunk_overlap: int = 20
    
    def split(self, text:str) -> List[str]:
        """
        将文本分割成chunk
        """
        chunks = []
        start = 0
        
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]            
            if chunk:
                chunks.append(chunk.strip())
            start = end - self.chunk_overlap
   
        return chunks

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class SimpleVectorStore:
    """
    简化的向量存储（使用TF-IDF）
    """
    def __init__(self):
        # 中文没有空格，默认分词器会把整句当成一个词（如 "什么是rag"），
        # 导致查询和文档几乎无法匹配；改用字符 n-gram 让中文也能正常召回。
        self.vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 3))
        self.vectors = None
        self.documents = []
    
    def add_documents(self, docs: List[Dict[str, Any]]):
        """
        添加文档到向量存储
        """
        texts = [doc['content'] for doc in docs]
        self.documents = docs
        self.vectors = self.vectorizer.fit_transform(texts)
    
    def search(self, query: str, top_k: int = 3) -> List[Dict]:
        """
        搜索相关文档
        """
        if self.vectors is None:
            return []
        
        query_vector = self.vectorizer.transform([query])
        
        similarities = cosine_similarity(query_vector, self.vectors)[0]
        
        top_indices = np.argsort(similarities)[::-1][:top_k]
        
        results = []
        for idx in top_indices:
            results.append({
                'document': self.documents[idx]['content'],
                'metadata': self.documents[idx]['metadata'],
                'score': float(similarities[idx])
            })
        
        return results

def rag_search(query: str, top_k: int = 3) -> str:
    """
    从本地知识库检索与 query 最相关的文本片段，返回可直接阅读的字符串。
    """
    docs = DocLoader(doc_dir).load() # type: ignore

    # 用 TextSplitter 把每篇文档切成 chunk 再建索引（原来 splitter 建了却没用到）
    splitter = TextSplitter(chunk_size=100, chunk_overlap=15)
    chunks: List[Dict[str, Any]] = []
    for doc in docs:
        for i, piece in enumerate(splitter.split(doc['content'])):
            chunks.append({
                'content': piece,
                'metadata': {**doc['metadata'], 'chunk': i},
            })

    vector_store = SimpleVectorStore()
    vector_store.add_documents(chunks)
    results = vector_store.search(query, top_k)

    # 工具必须返回 str，模型才能直接阅读；这里整理成带来源和分数的文本
    if not results:
        return "未找到相关内容。"

    return "\n\n".join(
        f"[{i + 1}] 来源: {r['metadata']['filename']} (score={r['score']:.3f})\n{r['document']}"
        for i, r in enumerate(results)
    )