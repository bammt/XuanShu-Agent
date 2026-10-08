"""Run artifact listing, naming and download responses.
"""
import asyncio
import mimetypes
from .config import (
    settings,
)
from .db import (
    Application,
    Run,
)
from .services import (
    app_dir,
    app_object_key,
    app_session_dir,
    app_session_object_key,
    minio,
    resolve_app_file,
    safe_relative_path,
)
from fastapi import (
    HTTPException,
)
from fastapi.responses import (
    StreamingResponse,
)
from pathlib import (
    Path,
)
from urllib.parse import (
    quote,
)


def artifact_documents(run: Run, app_row: Application | None, url_prefix: str,
                       url_query: str = '') -> list[dict]:
    if not app_row:
        return []
    documents = []
    execution_scope = str((run.approval_payload or {}).get('execution_scope') or '')
    for raw_name in (run.approval_payload or {}).get('artifacts', []):
        try:
            root = (app_session_dir(app_row.workspace_id, app_row.id, execution_scope, app_row.kind)
                    if execution_scope else app_dir(app_row.workspace_id, app_row.id, app_row.kind))
            local = resolve_app_file(root, raw_name)
            name = local.relative_to(root).as_posix()
            key = (app_session_object_key(
                app_row.workspace_id, app_row.id, execution_scope, name, app_row.kind,
            ) if execution_scope else app_object_key(app_row.workspace_id, app_row.id, name, app_row.kind))
        except ValueError:
            continue
        documents.append({
            'name': name,
            'size': local.stat().st_size if local.is_file() else None,
            'content_type': mimetypes.guess_type(name)[0] or 'application/octet-stream',
            'object_key': key,
            'minio_path': f'minio://{settings.minio_bucket}/{key}',
            'url': f'{url_prefix}/{quote(name, safe="/")}{url_query}',
        })
    return documents


def artifact_name(run: Run, filename: str) -> str:
    try:
        name = safe_relative_path(filename).as_posix()
    except ValueError as exc:
        raise HTTPException(404, '交付文件不存在') from exc
    if name not in (run.approval_payload or {}).get('artifacts', []):
        raise HTTPException(404, '交付文件不存在')
    return name


def artifact_object_key(run: Run, app_row: Application, name: str) -> str:
    execution_scope = str((run.approval_payload or {}).get('execution_scope') or '')
    if execution_scope:
        return app_session_object_key(
            app_row.workspace_id, app_row.id, execution_scope, name, app_row.kind,
        )
    return app_object_key(app_row.workspace_id, app_row.id, name, app_row.kind)


async def minio_download_response(object_key: str, filename: str) -> StreamingResponse:
    try:
        source = await asyncio.to_thread(minio.get_object, settings.minio_bucket, object_key)
    except Exception as exc:
        raise HTTPException(404, 'MinIO 中不存在该交付文件') from exc

    def chunks():
        try:
            while block := source.read(1024 * 1024):
                yield block
        finally:
            source.close()
            source.release_conn()

    media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    encoded = quote(Path(filename).name)
    # Image artifacts are consumed by RichMessage through an object URL. An
    # attachment disposition makes browsers treat the same response as a
    # download, so only non-image artifacts retain download semantics.
    disposition = 'inline' if media_type.startswith('image/') else 'attachment'
    return StreamingResponse(
        chunks(), media_type=media_type,
        headers={'Content-Disposition': f"{disposition}; filename*=UTF-8''{encoded}"},
    )
