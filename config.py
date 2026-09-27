import os
import pathlib
from dotenv import load_dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")

def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)

def env_path(name: str, default: str | None = None) -> pathlib.Path | None:
    """读取路径型环境变量。相对路径以 PROJECT_ROOT 为基准，与工作目录无关。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        if default is None:
            return None
        value = default
    path = pathlib.Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path

API_KEY = os.getenv("OPENCODE_API_KEY")
BASE_URL = os.getenv("BASE_URL")
MODEL = os.getenv("MODEL")
REASONING_EFFORT = (os.getenv("REASONING_EFFORT") or "").strip()
# Optional provider extension; auto sends no thinking override.
THINKING_MODE = (os.getenv("THINKING_MODE") or "auto").strip().lower()
if THINKING_MODE not in {"auto", "enabled", "disabled"}:
    raise ValueError("THINKING_MODE 必须为 auto、enabled 或 disabled")
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY")

DOC_DIR = env_path("DOC_DIR", "data/raw")
RAG_ASSESS = env_bool("RAG_ASSESS", False)

# Embedding 模型来源：LOCAL = 本地模型；API = 云端 OpenAI 兼容 /embeddings 接口
EMBEDDING_MODEL_SOURCE = (os.getenv("EMBEDDING_MODEL_SOURCE") or "LOCAL").upper()
EMBEDDING_MODEL_URL = os.getenv("EMBEDDING_MODEL_URL")
EMBEDDING_MODEL_API_KEY = os.getenv("EMBEDDING_MODEL_API_KEY")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-3-small")
EMBEDDING_LOCAL_PATH = env_path("EMBEDDING_LOCAL_PATH")

TIMEOUT = env_float("TIMEOUT", 30.0)
SESSION_TIMEOUT = env_float("SESSION_TIMEOUT", 60.0)
MAX_RETRIES = env_int("MAX_RETRIES", 2)
MODEL_CALL_TIMEOUT = max(1.0, env_float("MODEL_CALL_TIMEOUT", 90.0))
ASSESS_TIMEOUT = max(1.0, env_float("ASSESS_TIMEOUT", 30.0))

# 工具沙箱与命令执行
SANDBOX_DIR = env_path("SANDBOX_DIR", "crud_tests")   # 文件/命令操作的活动范围
CONFIRM_TIMEOUT = env_float("CONFIRM_TIMEOUT", 30.0)  # 风险操作确认等待秒数，超时视为拒绝
COMMAND_TIMEOUT = env_float("COMMAND_TIMEOUT", 10.0)  # 单条命令最长执行秒数
TOOL_MAX_OUTPUT = env_int("TOOL_MAX_OUTPUT", 20000)   # 工具输出（命令/文件内容）最大字符数，超出截断
FILE_READ_CHARS = max(1, env_int("FILE_READ_CHARS", 6000))
MODEL_INPUT_CHARS = max(1000, env_int("MODEL_INPUT_CHARS", 64000))
MODEL_OUTPUT_CHARS = max(1000, env_int("MODEL_OUTPUT_CHARS", 48000))
AUDIT_LOG = env_path("AUDIT_LOG", "logs/audit.log")   # 确认与执行审计日志

DEBUG = env_bool("DEBUG")

# RAG models and indexes are cached locally; secrets are never cache identifiers.
RAG_CACHE_DIR = env_path("RAG_CACHE_DIR", ".cache/rag")
RAG_CANDIDATES = env_int("RAG_CANDIDATES", 50)
RAG_RERANK_TOP_N = env_int("RAG_RERANK_TOP_N", 50)
RAG_RRF_K = env_int("RAG_RRF_K", 60)
RAG_PARENT_CHARS = env_int("RAG_PARENT_CHARS", 600)
RAG_CHILD_CHARS = env_int("RAG_CHILD_CHARS", 240)
RAG_BATCH_SIZE = env_int("RAG_BATCH_SIZE", 32)
RAG_THREADS = env_int("RAG_THREADS", 4)
RERANK_MODEL = os.getenv("RERANK_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
RERANK_REVISION = os.getenv("RERANK_REVISION")
RERANK_LOCAL_PATH = env_path("RERANK_LOCAL_PATH")
# Cross-encoder raw logits, not probabilities; configurable, model-specific gates.
RAG_THRESHOLD_STRICT = env_float("RAG_THRESHOLD_STRICT", 0.0)
RAG_THRESHOLD_NORMAL = env_float("RAG_THRESHOLD_NORMAL", -2.0)
RAG_THRESHOLD_LOOSE = env_float("RAG_THRESHOLD_LOOSE", -10.0)

# Durable user conversations and memory, separate from disposable model caches.
AGENT_STATE_DIR = env_path("AGENT_STATE_DIR", ".agent")
MAX_CONCURRENT_AGENTS = max(1, env_int("MAX_CONCURRENT_AGENTS", 4))
MAX_TOOL_ROUNDS = max(1, env_int("MAX_TOOL_ROUNDS", 24))

# 持久记忆写入上限；旧文件只限制注入，不改写。
MEMORY_MAX_CHARS = env_int("MEMORY_MAX_CHARS", 4000)

ASSESS_CONCURRENCY = env_int("ASSESS_CONCURRENCY", 1)

WEB_SEARCH_TIMEOUT = env_float("WEB_SEARCH_TIMEOUT", 15.0)
WEB_SEARCH_CONFIRM = os.getenv("WEB_SEARCH_CONFIRM", "always").lower()
