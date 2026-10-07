"""按模板整理本地环境配置，保留原始赋值且不输出秘密。"""
import argparse
from io import StringIO
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from dotenv.parser import parse_stream
from dotenv.variables import Variable, parse_variables


LOCAL_SECTION = '# ---- 本地额外设置与注释 ----'


def _read(path: Path, *, optional: bool = False) -> str | None:
    """同一描述符检查普通文件，拒绝链接及可能阻塞的 FIFO。"""
    if path.is_symlink():
        raise ValueError(f'{path.name} 不能是符号链接')
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        if optional:
            return None
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'{path.name} 必须是普通文件')
        try:
            return stream.read().decode('utf-8')
        except UnicodeError:
            raise ValueError(f'{path.name} 必须采用 UTF-8 编码') from None


def _parse(text: str, label: str):
    bindings = list(parse_stream(StringIO(text)))
    keys = {}
    for item in bindings:
        if item.error:
            raise ValueError(f'{label} 第 {item.original.line} 行语法无效')
        if item.key is None:
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', item.key):
            raise ValueError(f'{label} 第 {item.original.line} 行变量名无效')
        if item.key in keys:
            raise ValueError(f'{label} 重复配置键：{item.key}')
        keys[item.key] = item
    return bindings, keys


def _render(template_bindings, local_bindings, local_keys):
    chunks = []
    known = {item.key for item in template_bindings if item.key is not None}
    template_comments = {line.strip() for item in template_bindings if item.key is None
                         for line in item.original.string.splitlines() if line.strip()}
    for item in template_bindings:
        if item.key in local_keys:
            original = item.original.string
            prefix = original[:len(original) - len(original.lstrip('\r\n'))]
            chunks.append(prefix + local_keys[item.key].original.string.strip('\r\n') + '\n')
        else:
            chunks.append(item.original.string.rstrip('\r\n') + '\n')
    # 保留独有注释及额外键的相对顺序；模板注释和自动分组标题不重复追加。
    extras = []
    for item in local_bindings:
        if item.key is not None:
            if item.key not in known:
                extras.append(item.original.string.strip('\r\n'))
        else:
            for line in item.original.string.splitlines():
                if line.strip() and line.strip() not in template_comments and line.strip() != LOCAL_SECTION:
                    extras.append(line)
    if extras:
        chunks.append('\n' + LOCAL_SECTION + '\n' + '\n'.join(extras) + '\n')
    return ''.join(chunks)


def _resolved(bindings, environment, *, override):
    """沿用 dotenv 的变量原子与优先级，评估环境快照而不修改进程环境。"""
    values = {}
    for item in bindings:
        if item.key is None:
            continue
        if item.value is None:
            values[item.key] = None
        else:
            scope = dict(environment) if override else dict(values)
            scope.update(values if override else environment)
            values[item.key] = ''.join(atom.resolve(scope) for atom in parse_variables(item.value))
    return values


def _check_values(before, after):
    """当前环境不能掩盖引用次序变化，也覆盖无变量及独立变量的环境。"""
    references = {atom.name for item in [*before, *after] if item.value is not None
                  for atom in parse_variables(item.value) if isinstance(atom, Variable)}
    marker = uuid.uuid4().hex
    environments = [dict(os.environ), {}, {name: f'{marker}_{name}' for name in references}]
    for environment in environments:
        for override in (False, True):
            original = _resolved(before, environment, override=override)
            proposed = _resolved(after, environment, override=override)
            changed = [key for key, value in original.items() if proposed.get(key) != value]
            if changed:
                raise ValueError('重排会改变已有变量展开结果，请先检查引用顺序：' + ', '.join(changed))


def _write(target: Path, text: str, original: str | None):
    descriptor, temporary = tempfile.mkstemp(prefix='.env-sync-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # 发布前检查目标未变；不创建持有秘密的备份副本。
        if _read(target, optional=True) != original:
            raise ValueError('本地配置在同步期间发生变化，请重新运行')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_env(template: Path, target: Path, *, write: bool = False) -> dict:
    """检查/同步模板键与布局，返回键名和计数，不返回任何配置值。"""
    template, target = Path(template), Path(target)
    if template.resolve() == target.resolve():
        raise ValueError('模板与本地文件必须不同')
    reference = _read(template)
    original = _read(target, optional=True)
    template_bindings, template_keys = _parse(reference, '模板')
    if not template_keys:
        raise ValueError('模板不能为空')
    local_bindings, local_keys = _parse(original or '', '本地配置')
    rendered = _render(template_bindings, local_bindings, local_keys)
    proposed, _ = _parse(rendered, '同步结果')
    _check_values(local_bindings, proposed)
    report = {'changed': original != rendered,
              'added': [key for key in template_keys if key not in local_keys],
              'preserved': len(template_keys.keys() & local_keys.keys()),
              'local_only': [key for key in local_keys if key not in template_keys]}
    if write and (report['changed'] or stat.S_IMODE(target.stat().st_mode) != 0o600):
        _write(target, rendered, original)
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='同步 .env 的配置键与布局，保留本地值')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='只读检查（默认）')
    mode.add_argument('--write', action='store_true', help='原子同步本地文件')
    parser.add_argument('--template', type=Path, default=root / '.env.example')
    parser.add_argument('--target', type=Path, default=root / '.env')
    args = parser.parse_args()
    try:
        report = sync_env(args.template, args.target, write=args.write)
    except (OSError, ValueError) as exc:
        # 解析错误只含行号/键名；文件错误不包含任何读取出的行内容。
        print('配置同步失败：' + str(exc))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0 if args.write or not report['changed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
