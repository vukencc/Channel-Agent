import json
import math
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
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError(f"配置 {name} 必须为有限数字，请检查 .env") from None

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"配置 {name} 必须为整数，请检查 .env") from None

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
MODEL_CALL_TIMEOUT = env_float("MODEL_CALL_TIMEOUT", 90.0)
ASSESS_TIMEOUT = env_float("ASSESS_TIMEOUT", 30.0)

# 工具沙箱与命令执行
SANDBOX_DIR = env_path("SANDBOX_DIR", "crud_tests")   # 文件/命令操作的活动范围
CONFIRM_TIMEOUT = env_float("CONFIRM_TIMEOUT", 30.0)  # 风险操作确认等待秒数，超时视为拒绝
COMMAND_TIMEOUT = env_float("COMMAND_TIMEOUT", 10.0)  # 单条命令最长执行秒数
COMMAND_QUOTA_INTERVAL = env_float("COMMAND_QUOTA_INTERVAL", 0.1)  # 保持原 0.1s 采样间隔，避免输出触发额外扫描
TOOL_MAX_OUTPUT = env_int("TOOL_MAX_OUTPUT", 20000)   # 工具输出（命令/文件内容）最大字符数，超出截断
FILE_READ_CHARS = env_int("FILE_READ_CHARS", 6000)
MODEL_INPUT_CHARS = env_int("MODEL_INPUT_CHARS", 64000)
MODEL_OUTPUT_CHARS = env_int("MODEL_OUTPUT_CHARS", 48000)
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
MAX_CONCURRENT_AGENTS = env_int("MAX_CONCURRENT_AGENTS", 4)
MAX_TOOL_ROUNDS = env_int("MAX_TOOL_ROUNDS", 24)

# 持久记忆写入上限；旧文件只限制注入，不改写。
MEMORY_MAX_CHARS = env_int("MEMORY_MAX_CHARS", 4000)

ASSESS_CONCURRENCY = env_int("ASSESS_CONCURRENCY", 1)

WEB_SEARCH_TIMEOUT = env_float("WEB_SEARCH_TIMEOUT", 15.0)
WEB_SEARCH_CONFIRM = os.getenv("WEB_SEARCH_CONFIRM", "always").lower()

WORKSPACE_LIMIT_MB = env_int("WORKSPACE_LIMIT_MB", 512)
COMMAND_MEMORY_MB = env_int("COMMAND_MEMORY_MB", 512)
COMMAND_CPU_SECONDS = env_int("COMMAND_CPU_SECONDS", 5)
COMMAND_FILE_MB = env_int("COMMAND_FILE_MB", 32)
COMMAND_PROCESSES = env_int("COMMAND_PROCESSES", 128)


def validate_runtime_config() -> None:
    """启动前集中验证，不创建目录、不打印凭据。"""
    from urllib.parse import urlparse
    errors = []
    for name, value in [('OPENCODE_API_KEY', API_KEY), ('BASE_URL', BASE_URL), ('MODEL', MODEL)]:
        if not value or not value.strip():
            errors.append(f'{name} 缺失，请在 .env 配置')
    url = urlparse(BASE_URL or '')
    if url.scheme not in {'http', 'https'} or not url.hostname:
        errors.append('BASE_URL 必须是有效的 http/https 地址')
    for name, value in globals().items():
        if name.isupper() and type(value) in (int, float) and not name.startswith('RAG_THRESHOLD_') and name not in {'INPUT_COST_PER_MILLION', 'OUTPUT_COST_PER_MILLION'}:
            if not math.isfinite(value) or value <= 0:
                errors.append(f'{name} 必须大于 0 且有限')
    if not DOC_DIR or not DOC_DIR.is_dir() or not os.access(DOC_DIR, os.R_OK):
        errors.append('DOC_DIR 必须是可读的知识库目录')
    if not SANDBOX_DIR or (SANDBOX_DIR.exists() and not SANDBOX_DIR.is_dir()):
        errors.append('SANDBOX_DIR 必须是目录路径')
    if WEB_SEARCH_CONFIRM not in {'always', 'off'}:
        errors.append('WEB_SEARCH_CONFIRM 必须为 always 或 off')
    for name, value in [('INPUT_COST_PER_MILLION', INPUT_COST_PER_MILLION), ('OUTPUT_COST_PER_MILLION', OUTPUT_COST_PER_MILLION)]:
        if value < 0:
            errors.append(f'{name} 不能为负数')
    for endpoint in MODEL_FALLBACKS:
        if endpoint.get('base_url'):
            parsed = urlparse(endpoint['base_url'])
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                errors.append('MODEL_FALLBACKS 的 base_url 必须是 http/https 地址')
            if endpoint['base_url'] != BASE_URL and not endpoint.get('api_key_env'):
                errors.append('备用服务地址不同于 BASE_URL 时必须显式指定 api_key_env')
        if endpoint.get('api_key_env') and not os.getenv(endpoint['api_key_env']):
            errors.append('备用模型指定的 api_key_env 未配置')
        for name in ('input_cost_per_million', 'output_cost_per_million'):
            value = endpoint.get(name, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                errors.append(f'备用模型 {name} 必须为非负有限数字')
    if errors:
        raise ValueError('配置错误：\n- ' + '\n- '.join(errors))

ENABLE_DEBUG_TOOL = env_bool("ENABLE_DEBUG_TOOL", False)

MODEL_INPUT_TOKENS = env_int("MODEL_INPUT_TOKENS", 16000)
CONTEXT_SUMMARY = env_bool("CONTEXT_SUMMARY", True)
SUMMARY_INPUT_CHARS = env_int("SUMMARY_INPUT_CHARS", 12000)
SUMMARY_CHARS = env_int("SUMMARY_CHARS", 1000)
SUMMARY_TIMEOUT = env_float("SUMMARY_TIMEOUT", 10)

SESSION_MAX_MB = env_int("SESSION_MAX_MB", 64)

MAX_TOOL_CALLS_PER_ROUND = env_int("MAX_TOOL_CALLS_PER_ROUND", 8)
TOOL_CONCURRENCY = env_int("TOOL_CONCURRENCY", 4)
TOOL_TIMEOUT = env_float("TOOL_TIMEOUT", 120)

MEMORY_TOP_K = env_int("MEMORY_TOP_K", 4)
MEMORY_INJECT_CHARS = env_int("MEMORY_INJECT_CHARS", 1200)
MEMORY_AUTO_EXTRACT = env_bool("MEMORY_AUTO_EXTRACT", False)
MEMORY_SHARED = env_bool("MEMORY_SHARED", False)


try:
    MODEL_FALLBACKS = json.loads(os.getenv('MODEL_FALLBACKS', '[]'))
    if not isinstance(MODEL_FALLBACKS, list) or any(
            not isinstance(item, dict) or not isinstance(item.get('model'), str) or not item['model'].strip()
            for item in MODEL_FALLBACKS):
        raise ValueError
except (ValueError, TypeError):
    raise ValueError('MODEL_FALLBACKS 必须是包含 model 的 JSON 对象数组') from None
MODEL_STREAM_USAGE = env_bool('MODEL_STREAM_USAGE', False)
INPUT_COST_PER_MILLION = env_float('INPUT_COST_PER_MILLION', 0)
OUTPUT_COST_PER_MILLION = env_float('OUTPUT_COST_PER_MILLION', 0)

ASSESS_INPUT_CHARS = env_int("ASSESS_INPUT_CHARS", 12000)
ASSESS_CONTEXT_CHARS = env_int("ASSESS_CONTEXT_CHARS", 2000)

RUN_HISTORY_LIMIT = env_int("RUN_HISTORY_LIMIT", 20)
