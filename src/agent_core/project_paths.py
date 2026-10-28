"""不加载应用配置的项目根目录发现，供运行时与配置维护命令共用。"""
import os
from pathlib import Path
import tomllib


def project_root(source_file: str | Path) -> Path:
    """显式配置优先；源码使用工作区根，独立安装使用调用者当前目录。"""
    override = os.getenv('AI_AGENT_PROJECT_ROOT')
    if override:
        return Path(override).expanduser().resolve()
    source = Path(source_file).resolve()
    for candidate in source.parents:
        if not (candidate / '.env.example').is_file():
            continue
        try:
            project = tomllib.loads((candidate / 'pyproject.toml').read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        metadata = project.get('project')
        if not isinstance(metadata, dict) or metadata.get('name') != 'ai-agent-startup':
            continue
        table = project
        for key in ('tool', 'uv', 'workspace'):
            table = table.get(key) if isinstance(table, dict) else None
        members = table.get('members') if isinstance(table, dict) else None
        if not isinstance(members, list):
            continue
        # 安装在其他项目 .venv 中的 wheel 不能借用该项目的 .env 和状态根。
        for member in members:
            if not isinstance(member, str):
                continue
            member_root = (candidate / member).resolve()
            if (not member_root.is_relative_to(candidate)
                    or member_root.name not in ('agent_core', 'agent_test', 'agent_frontend')
                    or not source.is_relative_to(member_root)):
                continue
            # 成员根就是包；连续包路径排除成员内的虚拟环境、构建目录和回归测试。
            directory = source.parent
            while (directory / '__init__.py').is_file():
                if directory == member_root:
                    return candidate
                directory = directory.parent
    return Path.cwd().resolve()
