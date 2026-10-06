"""维护期间释放已加载的 RAG 资源，不触碰磁盘文件。"""
import sys


def clear_runtime_caches() -> None:
    """不额外导入模型，关闭运行时资源并清除已有缓存。

    调用方必须在重置及随后磁盘清理期间阻止检索和推理；下次检索按需重建。
    """
    for name in ('index', 'result_cache', 'text_store'):
        module = sys.modules.get(f'ai_agent_startup.rag.{name}')
        if module is not None:
            module.clear_runtime_cache()
    for name, caches in (
        ('embedding', ('_get_model', 'artifact_hash')),
        ('rerank', ('_get_reranker',)),
        ('lexical', ('_cached_tokens',)),
    ):
        module = sys.modules.get(f'ai_agent_startup.rag.{name}')
        if module is not None:
            for cache in caches:
                getattr(module, cache).cache_clear()
