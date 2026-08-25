import asyncio
import ctypes
import hashlib
import json
import os
import resource
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator


app = FastAPI(title='玄枢代码执行器', docs_url=None, redoc_url=None)
TIMEOUT = int(os.getenv('CODE_TIMEOUT_SECONDS', '60'))
MAX_SNAPSHOT_BYTES = int(os.getenv('EXECUTOR_MAX_SNAPSHOT_MB', '50')) * 1024 * 1024
SHARED_SECRET = os.getenv('EXECUTOR_SHARED_SECRET', '')
WORK_ROOT = Path(os.getenv('EXECUTOR_WORK_ROOT', '/var/lib/xuanshu/workspaces'))
EXECUTION_LOCKS: dict[tuple[int, str, int, str], asyncio.Lock] = {}
PYTHON_BOOTSTRAP = (
    'import os,sys;'
    'sys.path[:0]=[os.environ["PIP_TARGET"],os.environ["XUANSHU_SKILL_NAMESPACE_DIR"]];'
    'exec(compile(sys.argv[1], "<xuanshu-agent>", "exec"))'
)

# Landlock ABI 4+ restricts filesystem access while networking remains enabled.
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13
ACCESS_FS_TRUNCATE = 1 << 14
READ_EXECUTE = ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR
READ_WRITE_FILE = ACCESS_FS_READ_FILE | ACCESS_FS_WRITE_FILE
WORKSPACE_ACCESS = (
    READ_EXECUTE | ACCESS_FS_WRITE_FILE | ACCESS_FS_REMOVE_DIR | ACCESS_FS_REMOVE_FILE
    | ACCESS_FS_MAKE_DIR | ACCESS_FS_MAKE_REG | ACCESS_FS_MAKE_SOCK | ACCESS_FS_MAKE_FIFO
    | ACCESS_FS_MAKE_SYM | ACCESS_FS_REFER | ACCESS_FS_TRUNCATE
)
PR_SET_NO_NEW_PRIVS = 38
DELIVERABLE_SUFFIXES = {
    '.csv', '.doc', '.docx', '.html', '.jpeg', '.jpg', '.json', '.md', '.ods',
    '.odt', '.pdf', '.png', '.ppt', '.pptx', '.rtf', '.svg', '.txt', '.xls',
    '.xlsx', '.xml', '.zip',
}


class ExecuteIn(BaseModel):
    workspace_id: int = Field(gt=0)
    application_id: int
    application_kind: Literal['crew', 'flow'] = 'crew'
    execution_scope: str = Field(min_length=1, max_length=80)
    code: str = Field(default='', max_length=100_000)
    command: str = Field(default='', max_length=100_000)
    working_directory: str = Field(default='workspace', max_length=500)
    publish_outputs: bool = False
    skill_roots: list[str] = Field(default_factory=list, max_length=32)
    environment: dict[str, str] = Field(default_factory=dict, max_length=32)
    idempotency_key: str = Field(default='', max_length=240)

    @model_validator(mode='after')
    def exactly_one_program(self):
        if self.application_id == 0:
            raise ValueError('application_id 不能为 0')
        if bool(self.code.strip()) == bool(self.command.strip()):
            raise ValueError('必须且只能提供 code 或 command')
        return self


def _safe_skill_namespace(skill_id: str, slug: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]', '_', f'skill_{skill_id}_{slug}')[:160]


def _skill_import_map(app_root: Path, skill_roots: list[Path]) -> dict[str, str]:
    workspace = app_root / 'workspace'
    internal_root = workspace / '.xuanshu'
    namespace_root = internal_root / 'python' / 'xuanshu_skills'
    namespace_root.mkdir(parents=True, exist_ok=True)
    (namespace_root / '__init__.py').write_text(
        '# Generated Skill import index. Individual packages expose SKILL_ROOT.\n',
        encoding='utf-8',
    )
    entries: dict[str, dict] = {}
    manifest_path = internal_root / 'skills' / 'manifest.json'
    if manifest_path.is_file():
        try:
            entries = json.loads(manifest_path.read_text(encoding='utf-8')).get('skills') or {}
        except (OSError, json.JSONDecodeError):
            entries = {}
    entry_by_root = {
        (app_root / entry['discovery_root'] / entry['slug']).resolve(): entry
        for entry in entries.values()
        if entry.get('discovery_root') and entry.get('slug')
    }
    mapping: dict[str, str] = {}
    for skill_root in skill_roots:
        entry = entry_by_root.get(skill_root.resolve())
        skill_id = str((entry or {}).get('skill_id') or skill_root.parent.name.split('-', 1)[0])
        slug = str((entry or {}).get('slug') or skill_root.name)
        namespace = str((entry or {}).get('python_namespace') or _safe_skill_namespace(skill_id, slug))
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,159}', namespace):
            namespace = _safe_skill_namespace(skill_id, slug)
        package_dir = namespace_root / namespace
        package_dir.mkdir(parents=True, exist_ok=True)
        snapshot = skill_root.parent.name
        bridge = (
            'from pathlib import Path as _Path\n'
            f"SKILL_ROOT = (_Path(__file__).resolve().parents[3] / 'skills' / {snapshot!r} / {slug!r}).resolve()\n"
            '__path__ = [str(SKILL_ROOT)]\n'
        )
        (package_dir / '__init__.py').write_text(bridge, encoding='utf-8')
        mapping[namespace] = str(skill_root)
    return mapping


def isolated_environment(
    app_root: Path,
    requested: dict[str, str],
    workspace: Path | None = None,
    skill_roots: list[Path] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, str]:
    workspace = workspace or app_root
    skill_roots = skill_roots or []
    runtime_root = runtime_root or app_root / 'runtime'
    python_packages = runtime_root / 'python'
    runtime_bin = runtime_root / 'bin'
    for path in (python_packages, runtime_bin, runtime_root / 'cache', runtime_root / 'data', runtime_root / 'tmp'):
        path.mkdir(parents=True, exist_ok=True)
    skill_map = _skill_import_map(app_root, skill_roots)
    shared_internal = app_root / 'workspace' / '.xuanshu'
    skill_namespace_dir = shared_internal / 'python'
    python_path = [str(python_packages), str(skill_namespace_dir)]
    values = {
        'PATH': f'{runtime_bin}:/usr/local/bin:/usr/bin:/bin',
        'HOME': str(workspace),
        'PYTHONPATH': os.pathsep.join(python_path),
        'PIP_TARGET': str(python_packages),
        'TMPDIR': str(runtime_root / 'tmp'),
        'XDG_CACHE_HOME': str(runtime_root / 'cache'),
        'XDG_DATA_HOME': str(runtime_root / 'data'),
        'XUANSHU_WORKSPACE': str(workspace),
        'XUANSHU_SKILLS_DIR': str(shared_internal / 'skills'),
        'XUANSHU_SKILL_ROOTS': os.pathsep.join(str(path) for path in skill_roots),
        'XUANSHU_SKILL_MAP': json.dumps(skill_map, ensure_ascii=False, sort_keys=True),
        'XUANSHU_SKILL_NAMESPACE_DIR': str(skill_namespace_dir),
        'XUANSHU_RUNTIME_DIR': str(runtime_root),
        'LANG': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    for name, value in requested.items():
        if not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,127}', name):
            raise HTTPException(422, f'环境变量名无效：{name}')
        if name in values:
            raise HTTPException(422, f'不能覆盖隔离环境变量：{name}')
        if len(value) > 16_384:
            raise HTTPException(422, f'环境变量值过长：{name}')
        values[name] = value
    return values


class RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64), ('handled_access_net', ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def application_root(workspace_id: int, application_kind: str, application_id: int) -> Path:
    base = WORK_ROOT.resolve()
    root = (base / str(workspace_id) / '.xuanshu' / application_kind / str(application_id)).resolve()
    if base not in root.parents or not root.is_dir():
        raise HTTPException(404, '智能体工作目录不存在')
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        raise HTTPException(500, '智能体工作目录权限不足')
    return root


def application_workspace(workspace_id: int, application_id: int, application_kind: str = 'crew',
                          execution_scope: str = '') -> Path:
    root = application_root(workspace_id, application_kind, application_id)
    if execution_scope:
        scope_key = hashlib.sha256(execution_scope.encode('utf-8')).hexdigest()[:32]
        session_root = root / 'sessions' / scope_key
        workspace = session_root / 'workspace'
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = root / 'workspace'
    workspace = workspace.resolve()
    if root not in workspace.parents or not workspace.is_dir():
        raise HTTPException(404, '智能体工作目录不存在')
    return workspace


def resolve_working_directory(app_root: Path, requested: str,
                              workspace: Path | None = None) -> Path:
    pure = PurePosixPath(str(requested or 'workspace').replace('\\', '/'))
    if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
        raise HTTPException(422, '执行目录无效')
    if pure.parts[0] != 'workspace':
        raise HTTPException(422, '执行目录只能位于应用工作区')
    workspace = (workspace or (app_root / 'workspace')).resolve()
    target = (workspace / Path(*pure.parts[1:])).resolve()
    if target != workspace and workspace not in target.parents:
        raise HTTPException(422, '执行目录不存在或超出应用目录')
    if not target.is_dir():
        raise HTTPException(422, '执行目录不存在或超出应用目录')
    return target


def resolve_skill_roots(app_root: Path, requested: list[str]) -> list[Path]:
    allowed_root = (app_root / 'workspace' / '.xuanshu' / 'skills').resolve()
    roots: list[Path] = []
    for value in requested:
        pure = PurePosixPath(str(value).replace('\\', '/'))
        if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
            raise HTTPException(422, 'Skill 导入路径无效')
        target = (app_root / Path(*pure.parts)).resolve()
        if allowed_root not in target.parents or not (target / 'SKILL.md').is_file():
            raise HTTPException(422, 'Skill 导入路径不存在或超出当前应用')
        if target not in roots:
            roots.append(target)
    return roots


def public_workspace_files(root: Path):
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in {'memory', '.xuanshu'} or relative.parts[0].startswith('.'):
            continue
        yield path


def workspace_manifest(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): file_digest(path) for path in public_workspace_files(root)}


def tree_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): file_digest(path)
        for path in root.rglob('*') if path.is_file() and not path.is_symlink()
    }


def publish_skill_outputs(run_root: Path, baseline: dict[str, str], workspace: Path) -> list[str]:
    published = []
    for path in run_root.rglob('*'):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in DELIVERABLE_SUFFIXES:
            continue
        relative = path.relative_to(run_root)
        if relative.parts and (relative.parts[0].startswith('.') or '__pycache__' in relative.parts):
            continue
        digest = file_digest(path)
        if baseline.get(relative.as_posix()) == digest:
            continue
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        published.append(relative.as_posix())
    return published


def collect_changes(root: Path, baseline: dict[str, str]) -> tuple[list[dict], list[str]]:
    changed = []
    total = 0
    current: set[str] = set()
    for path in public_workspace_files(root):
        relative = path.relative_to(root).as_posix()
        current.add(relative)
        digest = file_digest(path)
        if baseline.get(relative) == digest:
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_SNAPSHOT_BYTES:
            raise HTTPException(413, '本次生成的交付文件超过执行器限制')
        changed.append({'path': relative, 'size': size})
    deleted = sorted(path for path in baseline if path not in current)
    return changed, deleted


def landlock_abi(libc) -> int:
    return int(libc.syscall(444, 0, 0, LANDLOCK_CREATE_RULESET_VERSION))


def add_path_rule(libc, ruleset_fd: int, path: Path, access: int) -> None:
    if not path.exists():
        return
    descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = PathBeneathAttr(access, descriptor)
        if libc.syscall(445, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0) != 0:
            raise OSError(ctypes.get_errno(), f'无法添加 Landlock 规则：{path}')
    finally:
        os.close(descriptor)


def sandbox_process(writable_roots: list[Path], readonly_roots: list[Path] | None = None) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if landlock_abi(libc) < 4:
        raise RuntimeError('宿主机 Landlock ABI 低于 4，拒绝不安全地执行代码')
    # Only the filesystem is restricted. Outbound networking remains available
    # so an application can fetch fonts, dependencies and other declared inputs.
    ruleset = RulesetAttr(WORKSPACE_ACCESS, 0)
    ruleset_fd = int(libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0))
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), '无法创建 Landlock ruleset')
    try:
        for system_path in ('/usr', '/lib', '/lib64', '/etc'):
            add_path_rule(libc, ruleset_fd, Path(system_path), READ_EXECUTE)
        add_path_rule(libc, ruleset_fd, Path('/dev/null'), READ_WRITE_FILE)
        for writable_root in writable_roots:
            add_path_rule(libc, ruleset_fd, writable_root, WORKSPACE_ACCESS)
        for readonly_root in readonly_roots or []:
            add_path_rule(libc, ruleset_fd, readonly_root, READ_EXECUTE)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), '无法启用 no_new_privs')
        if libc.syscall(446, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), '无法启用 Landlock')
    finally:
        os.close(ruleset_fd)
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


@app.get('/health')
async def health():
    libc = ctypes.CDLL(None, use_errno=True)
    abi = landlock_abi(libc)
    workspace_ready = WORK_ROOT.is_dir() and os.access(WORK_ROOT, os.R_OK | os.W_OK | os.X_OK)
    return {'status': 'ok' if abi >= 4 and bool(SHARED_SECRET) and workspace_ready else 'unsafe', 'landlock_abi': abi,
            'filesystem_isolation': abi >= 4, 'network_isolation': False,
            'network_access': True, 'authenticated': bool(SHARED_SECRET),
            'workspace_mounted': workspace_ready, 'workspace_root': str(WORK_ROOT)}


@app.post('/execute')
async def execute(body: ExecuteIn, x_executor_token: str = Header(default='')):
    if not SHARED_SECRET or x_executor_token != SHARED_SECRET:
        raise HTTPException(401, '执行器认证失败')
    app_root = application_root(body.workspace_id, body.application_kind, body.application_id)
    workspace = application_workspace(
        body.workspace_id, body.application_id, body.application_kind, body.execution_scope,
    )
    session_root = workspace.parent
    session_runtime = session_root / 'runtime'
    session_runtime.mkdir(parents=True, exist_ok=True)
    skill_roots = resolve_skill_roots(app_root, body.skill_roots)
    if body.publish_outputs:
        working_directory = resolve_working_directory(app_root, body.working_directory)
        skills_directory = (app_root / 'workspace' / '.xuanshu' / 'skills').resolve()
        if skills_directory not in working_directory.parents:
            raise HTTPException(422, '只有当前应用的 Skill 快照可以自动发布文件')
    else:
        working_directory = resolve_working_directory(app_root, body.working_directory, workspace)
    scope_key = hashlib.sha256(body.execution_scope.encode('utf-8')).hexdigest()[:32]
    lock = EXECUTION_LOCKS.setdefault(
        (body.workspace_id, body.application_kind, body.application_id, scope_key), asyncio.Lock(),
    )
    async with lock:
        receipt_path = None
        program_hash = hashlib.sha256(json.dumps({
            'code': body.code, 'command': body.command,
            'working_directory': body.working_directory,
            'publish_outputs': body.publish_outputs,
            'execution_scope': body.execution_scope,
            'skill_roots': body.skill_roots,
            'environment': body.environment,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if body.idempotency_key:
            receipt_root = session_runtime / 'idempotency'
            receipt_root.mkdir(parents=True, exist_ok=True)
            # A runtime node may legitimately revise a command after the model
            # receives a tool error.  Keep replay semantics for the exact same
            # program, but include the program digest in the receipt identity so
            # a changed command does not get misreported as a 409 conflict.
            receipt_name = hashlib.sha256(
                f'{body.idempotency_key}\0{program_hash}'.encode()
            ).hexdigest()
            receipt_path = receipt_root / f'{receipt_name}.json'
            legacy_path = receipt_root / f'{hashlib.sha256(body.idempotency_key.encode()).hexdigest()}.json'
            candidates = [receipt_path]
            if legacy_path != receipt_path:
                candidates.append(legacy_path)
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                try:
                    receipt = json.loads(candidate.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError) as exc:
                    raise HTTPException(500, '幂等执行记录损坏') from exc
                if receipt.get('program_hash') == program_hash:
                    return {**dict(receipt.get('result') or {}), 'idempotent_replay': True}
                # A legacy receipt with the same base key but a different
                # program is intentionally ignored; the digest-specific path
                # above will hold the new execution result.
        baseline = workspace_manifest(workspace)
        env = isolated_environment(
            app_root, body.environment, workspace, skill_roots, session_runtime,
        )
        if body.idempotency_key:
            env['XUANSHU_IDEMPOTENCY_KEY'] = body.idempotency_key
        run_copy = None
        run_baseline = {}
        process_directory = working_directory
        if body.publish_outputs:
            run_copy = session_runtime / 'skill-runs' / uuid.uuid4().hex
            run_copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(working_directory, run_copy)
            process_directory = run_copy
            run_baseline = tree_manifest(run_copy)
            env['XUANSHU_SKILL_ROOT'] = str(run_copy)
        try:
            program = (
                ('/bin/sh', '-c', body.command)
                if body.command
                else ('python', '-I', '-c', PYTHON_BOOTSTRAP, body.code)
            )
            proc = await asyncio.create_subprocess_exec(
                *program, cwd=process_directory, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=lambda: sandbox_process(
                    [session_root], [app_root / 'workspace' / '.xuanshu'],
                ),
            )
        except Exception as exc:
            if run_copy:
                shutil.rmtree(run_copy, ignore_errors=True)
            raise HTTPException(500, f'无法创建隔离进程：{exc}') from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT + 2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            if run_copy:
                shutil.rmtree(run_copy, ignore_errors=True)
            raise HTTPException(408, '代码执行超时')
        if run_copy:
            try:
                publish_skill_outputs(run_copy, run_baseline, workspace)
            finally:
                shutil.rmtree(run_copy, ignore_errors=True)
        changed, deleted = collect_changes(workspace, baseline)
        result = {'exit_code': proc.returncode, 'stdout': out.decode('utf-8', 'replace')[-100_000:],
                  'stderr': err.decode('utf-8', 'replace')[-100_000:], 'files': changed,
                  'deleted_files': deleted}
        if receipt_path:
            temporary = receipt_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({
                'idempotency_key': body.idempotency_key,
                'program_hash': program_hash,
                'result': result,
            }, ensure_ascii=False), encoding='utf-8')
            temporary.replace(receipt_path)
        return result
