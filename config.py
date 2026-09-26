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
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY")

DOC_DIR = env_path("DOC_DIR", "data/raw")
RAG_ASSESS = env_bool("RAG_ASSESS", False)

# Embedding 模型来源：LOCAL = 本地模型；API = 云端 OpenAI 兼容 /embeddings 接口
EMBEDDING_MODEL_SOURCE = (os.getenv("EMBEDDING_MODEL_SOURCE") or "LOCAL").upper()
EMBEDDING_MODEL_URL = os.getenv("EMBEDDING_MODEL_URL")
EMBEDDING_MODEL_API_KEY = os.getenv("EMBEDDING_MODEL_API_KEY")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL_NAME", "text-embedding-3-small")

TIMEOUT = env_float("TIMEOUT", 30.0)
SESSION_TIMEOUT = env_float("SESSION_TIMEOUT", 15.0)
MAX_RETRIES = env_int("MAX_RETRIES", 5)

# 工具沙箱与命令执行
SANDBOX_DIR = env_path("SANDBOX_DIR", "crud_tests")   # 文件/命令操作的活动范围
CONFIRM_TIMEOUT = env_float("CONFIRM_TIMEOUT", 30.0)  # 风险操作确认等待秒数，超时视为拒绝
COMMAND_TIMEOUT = env_float("COMMAND_TIMEOUT", 10.0)  # 单条命令最长执行秒数
TOOL_MAX_OUTPUT = env_int("TOOL_MAX_OUTPUT", 20000)   # 工具输出（命令/文件内容）最大字符数，超出截断
AUDIT_LOG = env_path("AUDIT_LOG", "logs/audit.log")   # 确认与执行审计日志

DEBUG = env_bool("DEBUG")
