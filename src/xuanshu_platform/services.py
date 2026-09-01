import base64, hashlib, io, json, os, re, shutil, uuid
from pathlib import Path, PurePosixPath
from minio import Minio
from minio.deleteobjects import DeleteObject
from redis.asyncio import Redis
import yaml
from .config import settings

redis = Redis.from_url(settings.redis_url, decode_responses=True)
RUN_QUEUE = 'xuanshu:run_queue'
KNOWLEDGE_QUEUE = 'xuanshu:knowledge_queue'
STUDIO_QUEUE = 'xuanshu:studio_queue'
STUDIO_PROCESSING_QUEUE = 'xuanshu:studio_processing_queue'
minio = Minio(settings.minio_endpoint, access_key=settings.minio_access_key, secret_key=settings.minio_secret_key,
              secure=settings.minio_secure)
WORK_ROOT = Path('/var/lib/xuanshu/workspaces')
INTERNAL_APP_DIRS = {'memory', '.xuanshu'}
APP_KINDS = {'crew', 'flow'}
BUILTIN_RESOURCES_ROOT = Path(__file__).with_name('builtin_resources')


def application_internal_dir(app_root: Path) -> Path:
    """Return internal resources stored inside the actual execution workspace."""
    return app_root / 'workspace' / '.xuanshu'


def validate_app_kind(app_kind: str) -> str:
    value = str(app_kind or '').lower()
    if value not in APP_KINDS:
        raise ValueError(f'不支持的应用类型：{app_kind}')
    return value


def composer_dir(user_id: int) -> Path:
    path = WORK_ROOT / 'composer' / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path
def app_root_dir(workspace_id: int, app_id: int, app_kind: str = 'crew') -> Path:
    path = WORK_ROOT / str(workspace_id) / '.xuanshu' / validate_app_kind(app_kind) / str(app_id)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path


def _merge_tree(source: Path, target: Path) -> None:
    """Move a legacy tree without overwriting newer destination files."""
    if not source.exists() or source.is_symlink():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in list(source.iterdir()):
        destination = target / item.name
        if item.is_symlink():
            item.unlink(missing_ok=True)
        elif item.is_dir():
            _merge_tree(item, destination)
            if item.exists():
                item.rmdir()
        elif not destination.exists():
            item.rename(destination)
        elif item.read_bytes() == destination.read_bytes():
            item.unlink()
        else:
            conflicts = target / '.migration-conflicts'
            conflicts.mkdir(exist_ok=True)
            item.rename(conflicts / f'{item.stem}-{uuid.uuid4().hex[:8]}{item.suffix}')
    if source.exists() and not any(source.iterdir()):
        source.rmdir()


def migrate_legacy_app_dir(workspace_id: int, app_id: int, app_kind: str = 'crew') -> Path:
    """Migrate pre-layout application files into one self-contained app root."""
    root = app_root_dir(workspace_id, app_id, app_kind)
    workspace = root / 'workspace'
    runtime = root / 'runtime'
    internal = workspace / '.xuanshu'
    skills = internal / 'skills'
    tools = internal / 'tools'
    for path in (workspace, runtime, skills, tools):
        path.mkdir(parents=True, exist_ok=True)

    # Versions before the execution-workspace layout stored generated resource
    # snapshots beside the workspace. Move them once so CrewAI discovery and
    # executed code see the same files rather than maintaining duplicate trees.
    previous_skills = root / 'skills'
    previous_tools = root / 'tools'
    if previous_skills.is_dir() and previous_skills.resolve() != skills.resolve():
        (previous_skills / 'manifest.json').unlink(missing_ok=True)
        _merge_tree(previous_skills, skills)
        if previous_skills.exists():
            shutil.rmtree(previous_skills, ignore_errors=True)
    if previous_tools.is_dir() and previous_tools.resolve() != tools.resolve():
        (previous_tools / 'manifest.json').unlink(missing_ok=True)
        _merge_tree(previous_tools, tools)
        if previous_tools.exists():
            shutil.rmtree(previous_tools, ignore_errors=True)

    legacy = WORK_ROOT / str(workspace_id) / str(app_id)
    if legacy.is_dir() and legacy.resolve() != root.resolve():
        legacy_internal = legacy / '.xuanshu'
        _merge_tree(legacy_internal / 'runtime', runtime)
        _merge_tree(legacy_internal / 'skills', skills / 'legacy')
        _merge_tree(legacy / 'memory', runtime / 'memory')
        if legacy_internal.exists():
            shutil.rmtree(legacy_internal, ignore_errors=True)
        _merge_tree(legacy, workspace)
        if legacy.exists():
            shutil.rmtree(legacy, ignore_errors=True)
    _merge_tree(WORK_ROOT / str(workspace_id) / f'{app_id}-memory', runtime / 'memory')
    return root


def relocate_app_root(workspace_id: int, app_id: int, old_kind: str, new_kind: str) -> Path:
    old_kind = validate_app_kind(old_kind)
    new_kind = validate_app_kind(new_kind)
    if old_kind == new_kind:
        return migrate_legacy_app_dir(workspace_id, app_id, new_kind)
    old_root = WORK_ROOT / str(workspace_id) / '.xuanshu' / old_kind / str(app_id)
    new_root = app_root_dir(workspace_id, app_id, new_kind)
    if old_root.is_dir():
        _merge_tree(old_root, new_root)
        shutil.rmtree(old_root, ignore_errors=True)
    return migrate_legacy_app_dir(workspace_id, app_id, new_kind)


def app_dir(workspace_id: int, app_id: int, app_kind: str = 'crew') -> Path:
    """Return the user-visible execution workspace for an application."""
    root = migrate_legacy_app_dir(workspace_id, app_id, app_kind)
    path = root / 'workspace'
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path


def execution_scope_key(execution_scope: str) -> str:
    """Map an opaque conversation/run identity to a filesystem-safe directory."""
    value = str(execution_scope or '').strip()
    if not value:
        raise ValueError('执行作用域不能为空')
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def app_session_root(workspace_id: int, app_id: int, execution_scope: str,
                     app_kind: str = 'crew') -> Path:
    root = migrate_legacy_app_dir(workspace_id, app_id, app_kind)
    path = root / 'sessions' / execution_scope_key(execution_scope)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path


def app_session_dir(workspace_id: int, app_id: int, execution_scope: str,
                    app_kind: str = 'crew') -> Path:
    """Return the writable workspace owned by one application conversation."""
    path = app_session_root(workspace_id, app_id, execution_scope, app_kind) / 'workspace'
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path


def app_session_runtime_dir(workspace_id: int, app_id: int, execution_scope: str,
                            app_kind: str = 'crew') -> Path:
    path = app_session_root(workspace_id, app_id, execution_scope, app_kind) / 'runtime'
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o770)
    return path


def app_runtime_dir(workspace_id: int, app_id: int, app_kind: str = 'crew') -> Path:
    path = migrate_legacy_app_dir(workspace_id, app_id, app_kind) / 'runtime'
    path.mkdir(parents=True, exist_ok=True)
    return path
def safe_name(name: str) -> str:
    cleaned = re.sub(r'[^\w.()（） -]+', '_', name, flags=re.UNICODE).strip(' .')
    return cleaned[:180] or 'upload'
def safe_relative_path(name: str) -> Path:
    pure = PurePosixPath(str(name).replace('\\', '/'))
    if pure.is_absolute() or not pure.parts or any(part in {'', '.', '..'} for part in pure.parts):
        raise ValueError(f'非法文件路径：{name}')
    cleaned = [safe_name(part) for part in pure.parts]
    return Path(*cleaned)
def visible_app_files(root: Path):
    """Yield user-visible files recursively while hiding runtime state."""
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and (relative.parts[0] in INTERNAL_APP_DIRS or relative.parts[0].startswith('.')):
            continue
        yield path
def execution_app_files(root: Path):
    """Files copied into one application's isolated execution workspace."""
    workspace = root / 'workspace' if (root / 'workspace').is_dir() else root
    yield from visible_app_files(workspace)
    for relative_root in (Path('workspace') / '.xuanshu' / 'skills', Path('runtime')):
        source_root = root / relative_root
        if not source_root.is_dir():
            continue
        for path in source_root.rglob('*'):
            if path.is_file() and not path.is_symlink():
                yield path
def app_file_manifest(root: Path) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): path.stat().st_mtime_ns for path in visible_app_files(root)}
def resolve_app_file(root: Path, relative_name: str) -> Path:
    raw = PurePosixPath(str(relative_name).replace('\\', '/'))
    if raw.is_absolute() or not raw.parts or any(part in {'', '.', '..'} for part in raw.parts):
        raise ValueError(f'非法文件路径：{relative_name}')
    if raw.parts[0] in INTERNAL_APP_DIRS or raw.parts[0].startswith('.'):
        raise ValueError('不能访问应用内部运行文件')
    relative = safe_relative_path(relative_name)
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise ValueError('文件路径超出应用目录')
    return target
def app_object_key(workspace_id: int, app_id: int, relative_name: str, app_kind: str = 'crew') -> str:
    root = app_dir(workspace_id, app_id, app_kind)
    relative = resolve_app_file(root, relative_name).relative_to(root)
    return f'{workspace_id}/{app_id}/{relative.as_posix()}'


def app_session_object_key(workspace_id: int, app_id: int, execution_scope: str,
                           relative_name: str, app_kind: str = 'crew') -> str:
    root = app_session_dir(workspace_id, app_id, execution_scope, app_kind)
    relative = resolve_app_file(root, relative_name).relative_to(root)
    scope_key = execution_scope_key(execution_scope)
    return f'{workspace_id}/{app_id}/conversations/{scope_key}/{relative.as_posix()}'
def parse_skill_manifest(text: str) -> dict:
    normalized = text.lstrip('\ufeff').replace('\r\n', '\n')
    if not normalized.startswith('---\n'):
        raise ValueError('SKILL.md 必须以 YAML front matter 开始')
    parts = normalized.split('\n---\n', 1)
    if len(parts) != 2:
        raise ValueError('SKILL.md 的 YAML front matter 缺少结束分隔线')
    try:
        metadata = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError as exc:
        raise ValueError(f'SKILL.md 的 YAML 无效：{exc}') from exc
    if not isinstance(metadata, dict):
        raise ValueError('SKILL.md 的 YAML front matter 必须是对象')
    name = str(metadata.get('name', '')).strip()
    description = str(metadata.get('description', '')).strip()
    instructions = parts[1].strip()
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name):
        raise ValueError('Skill name 必须使用小写英文、数字和单连字符')
    if not (10 <= len(description) <= 1000):
        raise ValueError('Skill description 需为 10-1000 个字符，并说明何时使用')
    if not instructions:
        raise ValueError('SKILL.md 必须包含可执行的正文指令')
    return {'name': name, 'description': description, 'instructions': instructions, 'metadata': metadata}

def validate_skill(text: str) -> tuple[bool, str]:
    try:
        parse_skill_manifest(text)
        return True, ''
    except ValueError as exc:
        return False, str(exc)

def materialize_skill(app_root: Path, skill_id: str, document: dict) -> Path:
    """Build an immutable standard Skill package and return its discovery root."""
    slug = str(document.get('slug') or '').strip()
    description = str(document.get('description') or '').strip()
    instructions = str(document.get('instructions') or '').strip()
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError(f'Skill {skill_id} 的 slug 不符合 CrewAI package 规范')
    if not description or not instructions:
        raise ValueError(f'Skill {skill_id} 缺少触发说明或正文指令')
    digest = hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    discovery_root = application_internal_dir(app_root) / 'skills' / f'{skill_id}-{digest}'
    skill_root = discovery_root / slug
    if (skill_root / 'SKILL.md').is_file():
        return discovery_root

    temporary_root = discovery_root.with_name(f'.tmp-{uuid.uuid4().hex}')
    temporary_skill = temporary_root / slug
    temporary_skill.mkdir(parents=True, exist_ok=False)
    metadata = {'name': slug, 'description': description}
    extra_metadata = {'author': document.get('author', 'local'), 'version': document.get('version', '1.0.0')}
    manifest = f"---\n{yaml.safe_dump({**metadata, 'metadata': extra_metadata}, allow_unicode=True, sort_keys=False)}---\n\n{instructions}\n"
    (temporary_skill / 'SKILL.md').write_text(manifest, encoding='utf-8')
    for item in document.get('files', []):
        relative = safe_relative_path(str(item.get('path') or ''))
        if relative.parts[0].startswith('.') or relative.parts[0] == 'SKILL.md':
            raise ValueError(f'Skill 资源路径无效：{relative.as_posix()}')
        target = temporary_skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = item.get('content', '')
        data = base64.b64decode(content, validate=True) if item.get('encoding') == 'base64' else str(content).encode()
        target.write_bytes(data)
        if item.get('executable'):
            target.chmod(0o750)
    discovery_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        temporary_root.rename(discovery_root)
    except OSError:
        shutil.rmtree(temporary_root, ignore_errors=True)
        if not (skill_root / 'SKILL.md').is_file():
            raise
    return discovery_root


def skill_python_namespace(skill_id: str, slug: str) -> str:
    """Return a stable, collision-resistant import namespace for one Skill."""
    raw = f"skill_{skill_id}_{slug}"
    namespace = re.sub(r'[^A-Za-z0-9_]', '_', raw)
    return namespace[:160]


def _materialize_skill_import_bridges(app_root: Path, entries: dict[str, dict]) -> None:
    """Expose Skill packages without putting colliding ``scripts`` dirs on sys.path."""
    namespace_root = application_internal_dir(app_root) / 'python' / 'xuanshu_skills'
    namespace_root.mkdir(parents=True, exist_ok=True)
    (namespace_root / '__init__.py').write_text(
        '# Generated Skill import index. Individual packages expose SKILL_ROOT.\n',
        encoding='utf-8',
    )
    active: set[str] = set()
    for entry in entries.values():
        namespace = skill_python_namespace(entry['skill_id'], entry['slug'])
        entry['python_namespace'] = namespace
        active.add(namespace)
        package_dir = namespace_root / namespace
        package_dir.mkdir(parents=True, exist_ok=True)
        discovery = Path(entry['discovery_root']).name
        slug = str(entry['slug'])
        # The bridge is tiny; the actual Skill files remain in one immutable snapshot.
        bridge = (
            'from pathlib import Path as _Path\n'
            f"SKILL_ROOT = (_Path(__file__).resolve().parents[3] / 'skills' / {discovery!r} / {slug!r}).resolve()\n"
            '__path__ = [str(SKILL_ROOT)]\n'
        )
        (package_dir / '__init__.py').write_text(bridge, encoding='utf-8')
    for candidate in namespace_root.iterdir():
        if candidate.name == '__init__.py' or candidate.name in active:
            continue
        if candidate.is_dir() and candidate.name.startswith('skill_'):
            shutil.rmtree(candidate, ignore_errors=True)


def _copy_platform_skill(app_root: Path) -> Path:
    source = BUILTIN_RESOURCES_ROOT / 'skills' / 'xuanshu-workspace-execution'
    destination = application_internal_dir(app_root) / 'skills' / 'platform' / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination.parent


def materialize_application_resources(
    app_root: Path,
    skill_documents: dict[str, dict],
    selected_skill_ids: set[str],
    *,
    include_code: bool,
    refresh: bool = False,
) -> dict:
    """Snapshot bound Skills into the same workspace used by executed code."""
    internal_root = application_internal_dir(app_root)
    skills_root = internal_root / 'skills'
    tools_root = internal_root / 'tools'
    skills_root.mkdir(parents=True, exist_ok=True)
    tools_root.mkdir(parents=True, exist_ok=True)
    manifest_path = skills_root / 'manifest.json'
    existing = {}
    if manifest_path.is_file() and not refresh:
        try:
            existing = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            existing = {}
    entries = {}
    for raw_id in sorted(selected_skill_ids, key=str):
        skill_id = str(raw_id)
        previous = (existing.get('skills') or {}).get(skill_id)
        previous_root = app_root / str((previous or {}).get('discovery_root', ''))
        if previous and (previous_root / str(previous.get('slug', '')) / 'SKILL.md').is_file():
            entries[skill_id] = previous
            continue
        document = skill_documents.get(skill_id)
        if not document:
            raise ValueError(f'应用绑定的 Skill {skill_id} 不存在')
        discovery = materialize_skill(app_root, skill_id, document)
        digest = discovery.name.split('-', 1)[-1]
        entries[skill_id] = {
            'skill_id': skill_id,
            'name': str(document.get('name') or document.get('slug') or skill_id),
            'slug': document['slug'],
            'digest': digest,
            'discovery_root': discovery.relative_to(app_root).as_posix(),
            'python_namespace': skill_python_namespace(skill_id, document['slug']),
            'has_scripts': any(str(item.get('path', '')).startswith('scripts/') for item in document.get('files', [])),
        }
    for entry in entries.values():
        entry.setdefault('python_namespace', skill_python_namespace(entry['skill_id'], entry['slug']))
    _materialize_skill_import_bridges(app_root, entries)
    platform_root = _copy_platform_skill(app_root)
    manifest = {
        'version': 2,
        'skills': entries,
        'platform_skill_root': platform_root.relative_to(app_root).as_posix() if platform_root else '',
        'workspace_root': 'workspace',
        'python_target': 'runtime/python',
        'python_namespace_root': 'workspace/.xuanshu/python',
    }
    temporary = manifest_path.with_name(f'.manifest-{uuid.uuid4().hex}.json')
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(manifest_path)
    tool_manifest = {
        'version': 2,
        'tools': [
            'read_document', 'read_spreadsheet',
            *(['execute_python', 'execute_command', 'execute_skill_script'] if include_code else []),
        ],
        'implementation': 'xuanshu_platform.tools.builtin',
    }
    if refresh:
        for candidate in tools_root.iterdir():
            if candidate.name != 'manifest.json':
                shutil.rmtree(candidate, ignore_errors=True) if candidate.is_dir() else candidate.unlink(missing_ok=True)
    (tools_root / 'manifest.json').write_text(json.dumps(tool_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    runtime_root = app_root / 'runtime'
    runtime_root.mkdir(parents=True, exist_ok=True)
    environment_manifest = {
        'version': 1,
        'workspace': 'workspace',
        'skills_directory': 'workspace/.xuanshu/skills',
        'skill_packages': [
            {'id': entry['skill_id'], 'digest': entry['digest'], 'root': entry['discovery_root'],
             'slug': entry['slug'], 'python_namespace': entry['python_namespace']}
            for entry in entries.values()
        ],
        'python_target': 'runtime/python',
        'network_access': True,
    }
    (runtime_root / 'environment.json').write_text(
        json.dumps(environment_manifest, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    if refresh:
        active_snapshots = {Path(entry['discovery_root']).name for entry in entries.values()}
        for candidate in skills_root.iterdir():
            if (candidate.is_dir() and candidate.name not in {'platform', 'legacy'}
                    and re.fullmatch(r'\d+-[0-9a-f]{16}', candidate.name)
                    and candidate.name not in active_snapshots):
                shutil.rmtree(candidate)
    return manifest


def application_skill_roots(app_root: Path, skill_ids: list[str], *, include_platform: bool = True) -> tuple[list[Path], dict[str, dict]]:
    manifest_path = application_internal_dir(app_root) / 'skills' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {'skills': {}}
    entries = manifest.get('skills') or {}
    roots = [app_root / entries[str(skill_id)]['discovery_root'] for skill_id in skill_ids if str(skill_id) in entries]
    platform = str(manifest.get('platform_skill_root') or '')
    if platform and include_platform:
        roots.append(app_root / platform)
    return roots, entries


def application_execution_skill_roots(
    app_root: Path,
    skill_ids: list[str],
    *,
    include_platform: bool = False,
) -> list[str]:
    """Return validated package roots relative to the application root.

    CrewAI still discovers complete Skills from these package roots. Python
    execution uses the generated ``xuanshu_skills.<namespace>`` bridges instead
    of adding every package root to ``PYTHONPATH``; this keeps same-named
    ``scripts`` and ``references`` directories independent.
    """
    discovery_roots, entries = application_skill_roots(
        app_root, skill_ids, include_platform=include_platform,
    )
    package_roots: list[Path] = []
    for skill_id in skill_ids:
        entry = entries.get(str(skill_id))
        if entry:
            package_roots.append(app_root / entry['discovery_root'] / entry['slug'])
    if include_platform and len(discovery_roots) > len(package_roots):
        platform_root = discovery_roots[-1]
        package_roots.extend(
            child for child in platform_root.iterdir()
            if child.is_dir() and (child / 'SKILL.md').is_file()
        )
    return [path.relative_to(app_root).as_posix() for path in package_roots]
async def ensure_bucket():
    def ensure() -> None:
        if not minio.bucket_exists(settings.minio_bucket):
            minio.make_bucket(settings.minio_bucket)
    await __import__('asyncio').to_thread(ensure)
async def store_upload(workspace_id:int, app_id:int, filename:str, data:bytes, app_kind: str = 'crew') -> str:
    key=f'{workspace_id}/{app_id}/{safe_name(filename)}'; await ensure_bucket()
    minio.put_object(settings.minio_bucket, key, io.BytesIO(data), len(data)); app_dir(workspace_id, app_id, app_kind).joinpath(safe_name(filename)).write_bytes(data); return key
def sync_app_file(workspace_id: int, app_id: int, relative_name: str, data: bytes, app_kind: str = 'crew') -> Path:
    root = app_dir(workspace_id, app_id, app_kind)
    target = resolve_app_file(root, relative_name)
    relative = target.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    key = app_object_key(workspace_id, app_id, relative.as_posix(), app_kind)
    minio.put_object(settings.minio_bucket, key, io.BytesIO(data), len(data))
    return target
def sync_existing_app_file(workspace_id: int, app_id: int, relative_name: str, app_kind: str = 'crew') -> None:
    root = app_dir(workspace_id, app_id, app_kind)
    source = resolve_app_file(root, relative_name)
    relative = source.relative_to(root)
    if not source.is_file():
        raise FileNotFoundError(relative.as_posix())
    size = source.stat().st_size
    with source.open('rb') as stream:
        minio.put_object(settings.minio_bucket, app_object_key(workspace_id, app_id, relative.as_posix(), app_kind), stream, size)
def delete_app_file(workspace_id: int, app_id: int, relative_name: str, app_kind: str = 'crew') -> None:
    root = app_dir(workspace_id, app_id, app_kind)
    target = resolve_app_file(root, relative_name)
    relative = target.relative_to(root)
    if target.is_file() or target.is_symlink():
        target.unlink(missing_ok=True)
    try:
        minio.remove_object(settings.minio_bucket, app_object_key(workspace_id, app_id, relative.as_posix(), app_kind))
    except Exception:
        pass


def sync_session_file(workspace_id: int, app_id: int, execution_scope: str,
                      relative_name: str, data: bytes, app_kind: str = 'crew') -> Path:
    root = app_session_dir(workspace_id, app_id, execution_scope, app_kind)
    target = resolve_app_file(root, relative_name)
    relative = target.relative_to(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    key = app_session_object_key(workspace_id, app_id, execution_scope, relative.as_posix(), app_kind)
    minio.put_object(settings.minio_bucket, key, io.BytesIO(data), len(data))
    return target


def sync_existing_session_file(workspace_id: int, app_id: int, execution_scope: str,
                               relative_name: str, app_kind: str = 'crew') -> None:
    root = app_session_dir(workspace_id, app_id, execution_scope, app_kind)
    source = resolve_app_file(root, relative_name)
    relative = source.relative_to(root)
    if not source.is_file():
        raise FileNotFoundError(relative.as_posix())
    size = source.stat().st_size
    key = app_session_object_key(workspace_id, app_id, execution_scope, relative.as_posix(), app_kind)
    with source.open('rb') as stream:
        minio.put_object(settings.minio_bucket, key, stream, size)


def delete_session_file(workspace_id: int, app_id: int, execution_scope: str,
                        relative_name: str, app_kind: str = 'crew') -> None:
    root = app_session_dir(workspace_id, app_id, execution_scope, app_kind)
    target = resolve_app_file(root, relative_name)
    relative = target.relative_to(root)
    if target.is_file() or target.is_symlink():
        target.unlink(missing_ok=True)
    try:
        minio.remove_object(
            settings.minio_bucket,
            app_session_object_key(workspace_id, app_id, execution_scope, relative.as_posix(), app_kind),
        )
    except Exception:
        pass


def remove_app_session(workspace_id: int, app_id: int, execution_scope: str,
                       app_kind: str = 'crew') -> None:
    root = migrate_legacy_app_dir(workspace_id, app_id, app_kind)
    target = root / 'sessions' / execution_scope_key(execution_scope)
    if target.is_dir():
        shutil.rmtree(target)
    remove_object_prefix(
        f'{workspace_id}/{app_id}/conversations/{execution_scope_key(execution_scope)}/'
    )
def remove_app_dir(workspace_id: int, app_id: int, app_kind: str | None = None) -> None:
    kinds = [validate_app_kind(app_kind)] if app_kind else sorted(APP_KINDS)
    targets = [WORK_ROOT / str(workspace_id) / '.xuanshu' / kind / str(app_id) for kind in kinds]
    targets.extend((WORK_ROOT / str(workspace_id) / str(app_id),
                    WORK_ROOT / str(workspace_id) / f'{app_id}-memory'))
    for target in targets:
        if target.exists():
            shutil.rmtree(target)

def remove_minio_prefix(workspace_id: int, app_id: int) -> None:
    prefixes = (f'{workspace_id}/{app_id}/', f'api-uploads/{app_id}/')
    names = [item.object_name for prefix in prefixes
             for item in minio.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)]
    if names:
        errors = list(minio.remove_objects(settings.minio_bucket, (DeleteObject(name) for name in names)))
        if errors:
            detail = '；'.join(f'{item.object_name}: {item.message}' for item in errors[:3])
            raise RuntimeError(f'MinIO 应用目录清理失败：{detail}')

def remove_object_prefix(prefix: str) -> None:
    names = [item.object_name for item in minio.list_objects(settings.minio_bucket, prefix=prefix, recursive=True)]
    if names:
        errors = list(minio.remove_objects(settings.minio_bucket, (DeleteObject(name) for name in names)))
        if errors:
            detail = '；'.join(f'{item.object_name}: {item.message}' for item in errors[:3])
            raise RuntimeError(f'MinIO 目录清理失败：{detail}')

def remove_workspace_dir(workspace_id: int) -> None:
    target = WORK_ROOT / str(workspace_id)
    if target.exists():
        shutil.rmtree(target)

def remove_composer_dir(user_id: int) -> None:
    target = WORK_ROOT / 'composer' / str(user_id)
    if target.exists():
        shutil.rmtree(target)
