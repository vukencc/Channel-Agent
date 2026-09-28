import json
import math
import os
import pathlib
import re
from dotenv import load_dotenv

def _project_root() -> pathlib.Path:
    """仓库根目录：优先 AI_AGENT_PROJECT_ROOT，其次从 src/ai_agent_startup/ 上溯，最后回退当前目录。"""
    override = os.getenv("AI_AGENT_PROJECT_ROOT")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    candidate = pathlib.Path(__file__).resolve().parents[2]
    return candidate if (candidate / "pyproject.toml").is_file() else pathlib.Path.cwd().resolve()


PROJECT_ROOT = _project_root()
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
ENABLE_COMMAND_JOBS = env_bool('ENABLE_COMMAND_JOBS', False)
COMMAND_JOB_MAX_SECONDS = env_float('COMMAND_JOB_MAX_SECONDS', 300)
COMMAND_JOB_CONCURRENCY = env_int('COMMAND_JOB_CONCURRENCY', 1)
COMMAND_JOB_MAX_ACTIVE = env_int('COMMAND_JOB_MAX_ACTIVE', 16)
COMMAND_JOB_LOG_BYTES = env_int('COMMAND_JOB_LOG_BYTES', 262144)
COMMAND_NETWORK = os.getenv('COMMAND_NETWORK', 'off').strip().lower()
COMMAND_NETWORK_ALLOWLIST = json.loads(os.getenv('COMMAND_NETWORK_ALLOWLIST', '[]'))
COMMAND_NETWORK_MAX_BYTES = env_int('COMMAND_NETWORK_MAX_BYTES', 8 * 1024 * 1024)
COMMAND_NETWORK_MAX_CONNECTIONS = env_int('COMMAND_NETWORK_MAX_CONNECTIONS', 4)
COMMAND_NETWORK_TIMEOUT = env_float('COMMAND_NETWORK_TIMEOUT', 5.0)
AUDIT_SYNC = env_bool("AUDIT_SYNC", False)  # 默认保持内核追加；开启后每事件 fsync
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
ENABLE_OBSERVABILITY = env_bool('ENABLE_OBSERVABILITY', False)
TRACE_MAX_ROWS = env_int('TRACE_MAX_ROWS', 20)
TRACE_MAX_FIELD_CHARS = env_int('TRACE_MAX_FIELD_CHARS', 512)
ENABLE_IMAGE_INPUT = env_bool('ENABLE_IMAGE_INPUT', False)
VISION_MODELS = json.loads(os.getenv('VISION_MODELS', '[]'))
IMAGE_MAX_BYTES = env_int('IMAGE_MAX_BYTES', 5242880)
IMAGE_MAX_PIXELS = env_int('IMAGE_MAX_PIXELS', 4000000)
IMAGE_MAX_PER_REQUEST = env_int('IMAGE_MAX_PER_REQUEST', 4)
IMAGE_TOKEN_BUDGET = env_int('IMAGE_TOKEN_BUDGET', 4096)
IMAGE_TOTAL_MB = env_int('IMAGE_TOTAL_MB', 32)
ENABLE_AGENT_TASKS = env_bool('ENABLE_AGENT_TASKS', False)
AGENT_TASK_CONCURRENCY = env_int('AGENT_TASK_CONCURRENCY', 2)
AGENT_TASK_MAX_ACTIVE = env_int('AGENT_TASK_MAX_ACTIVE', 16)
ENABLE_SESSION_BUDGETS = env_bool('ENABLE_SESSION_BUDGETS', False)
MODEL_RECOVERY_LIMIT = env_int('MODEL_RECOVERY_LIMIT', 1)
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
        if name.isupper() and type(value) in (int, float) and not name.startswith('RAG_THRESHOLD_') and name not in {'INPUT_COST_PER_MILLION', 'OUTPUT_COST_PER_MILLION', 'SESSION_COST_LIMIT', 'DAILY_COST_LIMIT', 'MODEL_REQUESTS_PER_MINUTE', 'RAG_RERANK_CACHE_SIZE', 'RAG_QUERY_CACHE_SIZE', 'RAG_MEMORY_LIMIT_MB', 'RAG_INFERENCE_CONCURRENCY', 'AUX_TIMEOUT', 'AUX_CONCURRENCY', 'MEMORY_CONCURRENCY', 'AUX_INPUT_COST_PER_MILLION', 'AUX_OUTPUT_COST_PER_MILLION', 'MODEL_RECOVERY_LIMIT'}:
            if not math.isfinite(value) or value <= 0:
                errors.append(f'{name} 必须大于 0 且有限')
    if not DOC_DIR or not DOC_DIR.is_dir() or not os.access(DOC_DIR, os.R_OK):
        errors.append('DOC_DIR 必须是可读的知识库目录')
    if not SANDBOX_DIR or (SANDBOX_DIR.exists() and not SANDBOX_DIR.is_dir()):
        errors.append('SANDBOX_DIR 必须是目录路径')
    if RAG_VECTOR_BACKEND not in {'exact', 'ann'}:
        errors.append('RAG_VECTOR_BACKEND 必须是 exact/ann')
    if RAG_RERANK_DEVICE not in {'cpu', 'cuda', 'mps', 'auto'}:
        errors.append('RAG_RERANK_DEVICE 必须是 cpu/cuda/mps/auto')
    if RAG_RERANK_DTYPE not in {'fp32', 'fp16'}:
        errors.append('RAG_RERANK_DTYPE 必须是 fp32/fp16')
    from ai_agent_startup.tools.network_proxy import validate_network_config
    try:
        validate_network_config()
    except ValueError as exc:
        errors.append(str(exc))
    if not isinstance(VISION_MODELS, list) or any(not isinstance(name, str) or not name.strip() for name in VISION_MODELS):
        errors.append('VISION_MODELS 必须为模型名称列表')
    if ENABLE_AGENT_TASKS and not ENABLE_SESSION_BUDGETS:
        errors.append('ENABLE_AGENT_TASKS 需要 ENABLE_SESSION_BUDGETS')
    if not 0 <= MODEL_RECOVERY_LIMIT <= 3:
        errors.append('MODEL_RECOVERY_LIMIT 必须在 0..3 之间')
    if WEB_SEARCH_CONFIRM not in {'always', 'off'}:
        errors.append('WEB_SEARCH_CONFIRM 必须为 always 或 off')
    for name, value in [('INPUT_COST_PER_MILLION', INPUT_COST_PER_MILLION), ('OUTPUT_COST_PER_MILLION', OUTPUT_COST_PER_MILLION)]:
        if value < 0:
            errors.append(f'{name} 不能为负数')
    for name in ('SESSION_COST_LIMIT', 'DAILY_COST_LIMIT', 'MODEL_REQUESTS_PER_MINUTE', 'RAG_RERANK_CACHE_SIZE', 'RAG_QUERY_CACHE_SIZE', 'RAG_MEMORY_LIMIT_MB', 'RAG_INFERENCE_CONCURRENCY', 'AUX_TIMEOUT', 'AUX_CONCURRENCY', 'MEMORY_CONCURRENCY', 'AUX_INPUT_COST_PER_MILLION', 'AUX_OUTPUT_COST_PER_MILLION'):
        if not math.isfinite(globals()[name]) or globals()[name] < 0:
            errors.append(f'{name} 必须为非负有限数字')
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
ENABLE_FILE_EXTRAS = env_bool("ENABLE_FILE_EXTRAS", False)

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
ENABLE_MEMORY_MANAGEMENT = env_bool('ENABLE_MEMORY_MANAGEMENT', False)
ENABLE_SESSION_BRANCHES = env_bool('ENABLE_SESSION_BRANCHES', False)
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
SESSION_COST_LIMIT = env_float('SESSION_COST_LIMIT', 0)
DAILY_COST_LIMIT = env_float('DAILY_COST_LIMIT', 0)
MODEL_REQUESTS_PER_MINUTE = env_int('MODEL_REQUESTS_PER_MINUTE', 0)

# 只允许已声明的请求参数；不接受任意网络地址或安全策略覆盖。
from ai_agent_startup.core.model_settings import ModelParameters, ModelProfile
from ai_agent_startup.core.permissions import PermissionRule
TOOL_PERMISSION_POLICY = os.getenv('TOOL_PERMISSION_POLICY', 'standard')
if TOOL_PERMISSION_POLICY not in {'readonly', 'standard', 'trusted'}:
    raise ValueError('TOOL_PERMISSION_POLICY 必须为 readonly/standard/trusted')
try:
    _permission_rules = json.loads(os.getenv('TOOL_PERMISSION_RULES', '[]'))
    if not isinstance(_permission_rules, list):
        raise ValueError
    TOOL_PERMISSION_RULES = [PermissionRule.model_validate(value).model_dump() for value in _permission_rules]
except (ValueError, TypeError):
    raise ValueError('TOOL_PERMISSION_RULES 必须为有效的工具/路径或命令 argv 前缀规则数组') from None
try:
    MODEL_PARAMETERS = ModelParameters.model_validate_json(os.getenv('MODEL_PARAMETERS', '{}')).model_dump(exclude_none=True)
    _profiles = json.loads(os.getenv('MODEL_PROFILES', '{}'))
    if not isinstance(_profiles, dict):
        raise ValueError
    MODEL_PROFILES = {name: ModelProfile.model_validate(value).model_dump(exclude_none=True)
                      for name, value in _profiles.items()}
except (ValueError, TypeError):
    raise ValueError('MODEL_PARAMETERS / MODEL_PROFILES 参数无效，请检查采样范围及支持字段') from None

FILE_APPEND_CHARS = env_int('FILE_APPEND_CHARS', 4000)
RAG_RETRY_LIMIT = env_int('RAG_RETRY_LIMIT', 2)
SYSTEM_PROMPT_FILE = env_path('SYSTEM_PROMPT_FILE')

RAG_RERANK_DEVICE = os.getenv('RAG_RERANK_DEVICE', 'cpu')
RAG_RERANK_DTYPE = os.getenv('RAG_RERANK_DTYPE', 'fp32')
RAG_RERANK_CACHE_SIZE = env_int('RAG_RERANK_CACHE_SIZE', 0)
RAG_QUERY_CACHE_SIZE = env_int('RAG_QUERY_CACHE_SIZE', 0)
RAG_VECTOR_BACKEND = os.getenv('RAG_VECTOR_BACKEND', 'exact')
RAG_ANN_MIN_CHILDREN = env_int('RAG_ANN_MIN_CHILDREN', 10000)
RAG_ANN_M = env_int('RAG_ANN_M', 16)
RAG_ANN_EF_CONSTRUCTION = env_int('RAG_ANN_EF_CONSTRUCTION', 200)
RAG_ANN_EF_SEARCH = env_int('RAG_ANN_EF_SEARCH', 256)
RAG_BM25_PERSIST = env_bool('RAG_BM25_PERSIST', False)
RAG_MEMORY_LIMIT_MB = env_int('RAG_MEMORY_LIMIT_MB', 0)
RAG_INFERENCE_CONCURRENCY = env_int('RAG_INFERENCE_CONCURRENCY', 0)
RAG_MAX_TOP_K = env_int('RAG_MAX_TOP_K', 50)
try:
    RAG_SOURCES = json.loads(os.getenv('RAG_SOURCES', '{}'))
    if not isinstance(RAG_SOURCES, dict):
        raise ValueError
    for name, entry in RAG_SOURCES.items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or not isinstance(entry, dict)
                or set(entry) - {'path', 'thresholds'} or not isinstance(entry.get('path'), str)
                or not entry['path'].strip()):
            raise ValueError
        entry['path'] = str((PROJECT_ROOT / pathlib.Path(entry['path']).expanduser()).resolve())
        if 'thresholds' in entry:
            values = entry['thresholds']
            if (not isinstance(values, dict) or set(values) != {'strict', 'normal', 'loose'}
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in values.values())
                    or not values['strict'] > values['normal'] > values['loose']):
                raise ValueError
except (ValueError, TypeError, OSError):
    raise ValueError('RAG_SOURCES 必须是命名知识库到 path/可选 thresholds 的 JSON 对象') from None
MODEL_STABLE_PREFIX = env_bool('MODEL_STABLE_PREFIX', False)
AUX_MODEL = os.getenv('AUX_MODEL', '').strip()
AUX_TIMEOUT = env_float('AUX_TIMEOUT', 0)
AUX_CONCURRENCY = env_int('AUX_CONCURRENCY', 0)
MEMORY_CONCURRENCY = env_int('MEMORY_CONCURRENCY', 0)
SUMMARY_CONCURRENCY = env_int('SUMMARY_CONCURRENCY', 1)
CONTEXT_SUMMARY_BACKGROUND = env_bool('CONTEXT_SUMMARY_BACKGROUND', False)
AUX_INPUT_COST_PER_MILLION = env_float('AUX_INPUT_COST_PER_MILLION', 0)
AUX_OUTPUT_COST_PER_MILLION = env_float('AUX_OUTPUT_COST_PER_MILLION', 0)
try:
    TOOL_ROOTS = json.loads(os.getenv('TOOL_ROOTS', '{}'))
    if not isinstance(TOOL_ROOTS, dict):
        raise ValueError
    for name, entry in TOOL_ROOTS.items():
        if (not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or name == 'workspace'
                or not isinstance(entry, dict) or set(entry) != {'path', 'read_only'}
                or entry['read_only'] is not True or not isinstance(entry['path'], str) or not entry['path'].strip()):
            raise ValueError
        path = pathlib.Path(entry['path']).expanduser()
        entry['path'] = str((PROJECT_ROOT / path).resolve())
except (ValueError, TypeError, OSError):
    raise ValueError('TOOL_ROOTS 必须是命名只读目录，例如 {"docs":{"path":"docs","read_only":true}}') from None
try:
    AUX_MODEL_PARAMETERS = ModelParameters.model_validate_json(os.getenv('AUX_MODEL_PARAMETERS', '{}')).model_dump(exclude_none=True)
except ValueError:
    raise ValueError('AUX_MODEL_PARAMETERS 参数无效') from None
try:
    MODEL_TOOL_NAMES = json.loads(os.getenv('MODEL_TOOL_NAMES', 'null'))
    if MODEL_TOOL_NAMES is not None and (not isinstance(MODEL_TOOL_NAMES, list)
            or any(not isinstance(name, str) for name in MODEL_TOOL_NAMES)):
        raise ValueError
except (ValueError, TypeError):
    raise ValueError('MODEL_TOOL_NAMES 必须是工具名 JSON 数组或 null（全部）') from None
try:
    RAG_RERANK_BY_BREADTH = json.loads(os.getenv('RAG_RERANK_BY_BREADTH', '{}'))
    if (not isinstance(RAG_RERANK_BY_BREADTH, dict)
            or any(key not in {'narrow', 'normal', 'wide'} or type(value) is not int or value < 1
                   for key, value in RAG_RERANK_BY_BREADTH.items())):
        raise ValueError
except (ValueError, TypeError):
    raise ValueError('RAG_RERANK_BY_BREADTH 必须是 breadth 到正整数候选数的 JSON 对象') from None
