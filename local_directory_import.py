"""
Local Directory Import Plugin for Open WebUI.

Bulk-imports files from a local drop folder into knowledge bases.
Each immediate subfolder of the drop folder is auto-mapped to a knowledge base
named after it (created if it does not exist). Files are copied to UPLOAD_DIR,
registered in the database, linked to the corresponding KB, and vectorized.

Admin-only access. The drop folder path is configured via Valves.
Returns a JSON summary with per-KB breakdowns.

Note: local filesystem only — not compatible with S3/GCS/Azure storage backends.
"""

__version__ = '0.1.1'

import asyncio
import hashlib
import inspect
import importlib
import json
import logging
import mimetypes
import pathlib
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy import select

UPLOAD_DIR = None
get_async_db = None
File = None
FileForm = None
Files = None
Knowledge = None
KnowledgeForm = None
Knowledges = None
UserModel = None
ProcessFileForm = None
process_file = None


def _get_mod(key: str):
    """Return an already-loaded module from sys.modules, or import it fresh."""
    mod = sys.modules.get(key)
    if mod is not None:
        return mod
    return importlib.import_module(key)


def _ensure_openwebui_imports() -> None:
    """Resolve Open WebUI symbols lazily for wider version compatibility.

    Uses sys.modules first to avoid re-importing modules that are already
    loaded by the running Open WebUI process (which would cause SQLAlchemy
    duplicate table errors). Falls back to importlib for each prefix in turn.
    """
    global UPLOAD_DIR
    global get_async_db
    global File
    global FileForm
    global Files
    global Knowledge
    global KnowledgeForm
    global Knowledges
    global UserModel
    global ProcessFileForm
    global process_file

    if all(
        sym is not None
        for sym in (
            UPLOAD_DIR,
            get_async_db,
            File,
            FileForm,
            Files,
            Knowledge,
            KnowledgeForm,
            Knowledges,
            ProcessFileForm,
            process_file,
        )
    ):
        return

    # Prefixes to try in order. backend.open_webui is intentionally omitted:
    # it aliases the same already-loaded modules and causes SQLAlchemy to
    # complain about duplicate table definitions.
    prefixes = ('open_webui', 'apps.webui')

    # db getter names vary across Open WebUI versions
    db_getter_names = ('get_async_db', 'get_session', 'get_db')

    errors = []
    for prefix in prefixes:
        try:
            config_mod = _get_mod(f'{prefix}.config')
            db_mod = _get_mod(f'{prefix}.internal.db')
            files_mod = _get_mod(f'{prefix}.models.files')
            knowledge_mod = _get_mod(f'{prefix}.models.knowledge')
            retrieval_mod = _get_mod(f'{prefix}.routers.retrieval')

            users_mod = None
            try:
                users_mod = _get_mod(f'{prefix}.models.users')
            except Exception:
                pass

            UPLOAD_DIR = getattr(config_mod, 'UPLOAD_DIR', None)

            # Accept whichever async-db getter this version exposes
            _get_async_db = None
            for name in db_getter_names:
                _get_async_db = getattr(db_mod, name, None)
                if _get_async_db is not None:
                    break
            get_async_db = _get_async_db

            File = getattr(files_mod, 'File', None)
            FileForm = getattr(files_mod, 'FileForm', None)
            Files = getattr(files_mod, 'Files', None)
            Knowledge = getattr(knowledge_mod, 'Knowledge', None)
            KnowledgeForm = getattr(knowledge_mod, 'KnowledgeForm', None)
            Knowledges = getattr(knowledge_mod, 'Knowledges', None)
            UserModel = getattr(users_mod, 'UserModel', None) if users_mod else None
            ProcessFileForm = getattr(retrieval_mod, 'ProcessFileForm', None)
            process_file = getattr(retrieval_mod, 'process_file', None)

            required = {
                'UPLOAD_DIR': UPLOAD_DIR,
                'get_async_db': get_async_db,
                'File': File,
                'FileForm': FileForm,
                'Files': Files,
                'KnowledgeForm': KnowledgeForm,
                'Knowledges': Knowledges,
                'process_file': process_file,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                errors.append(f'{prefix}: missing {", ".join(missing)}')
                continue

            return
        except Exception as exc:
            errors.append(f'{prefix}: {exc}')

    raise ImportError(
        'Unable to resolve Open WebUI tool imports for this environment. '
        + ' | '.join(errors)
    )

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ImportFileResult:
    relative_path: str
    filename: str
    file_id: str | None
    status: str
    error: str | None = None


@dataclass
class KBImportSummary:
    kb_name: str
    knowledge_id: str | None
    kb_created: bool
    discovered: int
    imported: int
    linked: int
    processed: int
    failed: int
    skipped: int = 0
    files: list = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    files_per_second: float = 0.0


@dataclass
class ImportSummary:
    drop_folder: str
    total_discovered: int
    total_imported: int
    total_linked: int
    total_processed: int
    total_failed: int
    total_skipped: int = 0
    knowledge_bases: list = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    files_per_second: float = 0.0


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _hash_file(path: pathlib.Path, chunk_size: int = 65536) -> str:
    """Return the hex SHA-256 digest of the file at *path*."""
    h = hashlib.sha256()
    with path.open('rb') as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _db_execute(db, statement):
    """Execute a statement across async and sync SQLAlchemy session shapes."""
    result = db.execute(statement)
    if inspect.isawaitable(result):
        return await result
    return result


async def _maybe_await(value):
    """Await *value* when needed, otherwise return it directly."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _find_file_by_hash(file_hash: str, db) -> 'File | None':
    """Return the first File record whose hash matches *file_hash*, or None."""
    _ensure_openwebui_imports()
    result = await _db_execute(
        db,
        select(File).where(File.hash == file_hash).limit(1)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _is_hidden_dir(name: str) -> bool:
    """Return True for dot-prefixed directory names except ``.attachments``."""
    return name.startswith('.') and name != '.attachments'


def _discover_subfolders(drop_folder: pathlib.Path) -> list:
    """Return a sorted list of immediate subdirectories inside *drop_folder*.

    All dot-prefixed directories (e.g. ``.git``) are excluded except
    ``.attachments``, which is a conventional image/attachment store in
    many Markdown repositories.
    """
    return sorted(
        [p for p in drop_folder.iterdir() if p.is_dir() and not _is_hidden_dir(p.name)]
    )


def _is_supported_import_file(path: pathlib.Path) -> bool:
    """Return True when *path* is an allowed text/structured doc file."""
    suffix = path.suffix.lower()
    return suffix in {
        '.md',
        '.markdown',
        '.mdown',
        '.mkd',
        '.txt',
        '.json',
        '.yml',
        '.yaml',
        '.pdf',
        '.png',
        '.svg',
        '.jpg',
        '.jpeg',
        '.xml',
        '.mmd',          # Mermaid diagram source
        '.mermaid',      # Mermaid diagram source (alternate extension)
        '.py',           # Python scripts
        '.ps1',          # PowerShell scripts
        '.html',         # HTML documentation
    }


def _discover_files(subfolder: pathlib.Path) -> list:
    """Return supported doc files recursively inside *subfolder*.

    Files are excluded when any intermediate directory component would be
    excluded by ``_is_hidden_dir`` (i.e. dot-prefixed except ``.attachments``).
    """
    base_parts = len(subfolder.parts)
    all_entries = list(subfolder.rglob('*'))
    all_files = [p for p in all_entries if p.is_file()]
    hidden_excluded = [
        p for p in all_files
        if any(_is_hidden_dir(part) for part in p.parts[base_parts:])
    ]
    unsupported_excluded = [
        p for p in all_files
        if not any(_is_hidden_dir(part) for part in p.parts[base_parts:])
        and not _is_supported_import_file(p)
    ]
    result = sorted(
        [
            p
            for p in all_files
            if _is_supported_import_file(p)
            and not any(_is_hidden_dir(part) for part in p.parts[base_parts:])
        ]
    )
    log.info(
        'local_import discover subfolder=%s rglob_entries=%d total_files=%d '
        'hidden_excluded=%d unsupported_excluded=%d will_import=%d',
        subfolder,
        len(all_entries),
        len(all_files),
        len(hidden_excluded),
        len(unsupported_excluded),
        len(result),
    )
    if hidden_excluded:
        log.info(
            'local_import discover subfolder=%s hidden_excluded_paths=%s',
            subfolder.name,
            [str(p.relative_to(subfolder)) for p in hidden_excluded[:20]],
        )
    if unsupported_excluded:
        extensions = sorted({p.suffix.lower() for p in unsupported_excluded})
        by_ext = {}
        for p in unsupported_excluded:
            ext = p.suffix.lower()
            by_ext.setdefault(ext, []).append(p)
        
        ext_summary = ', '.join(
            f'{ext}({len(by_ext[ext])} files)' 
            for ext in sorted(by_ext.keys())
        )
        log.warning(
            'local_import discover subfolder=%s UNSUPPORTED_FILES: %s (rerun with more extensions if needed)',
            subfolder.name,
            ext_summary,
        )
        log.info(
            'local_import discover subfolder=%s unsupported_example_files=%s',
            subfolder.name,
            [str(p.relative_to(subfolder)) for p in unsupported_excluded[:10]],
        )
    return result


# ---------------------------------------------------------------------------
# File staging helpers
# ---------------------------------------------------------------------------


def _copy_file_to_upload_dir(src: pathlib.Path, file_id: str, filename: str) -> pathlib.Path:
    """Copy *src* into UPLOAD_DIR with a prefixed name and return the destination path."""
    _ensure_openwebui_imports()
    dest = pathlib.Path(UPLOAD_DIR) / f'{file_id}_{filename}'
    shutil.copy(src, dest)
    return dest


async def _insert_file_record(
    user_id: str,
    file_id: str,
    filename: str,
    dest_path: pathlib.Path,
    relative_path: str,
    file_hash: str,
) -> None:
    """Create a File DB record for the staged file."""
    _ensure_openwebui_imports()
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    size = dest_path.stat().st_size
    await _maybe_await(
        Files.insert_new_file(
            user_id,
            FileForm(
                id=file_id,
                hash=file_hash,
                filename=filename,
                path=str(dest_path),
                data={'content': ''},
                meta={
                    'name': filename,
                    'content_type': content_type,
                    'size': size,
                    'source': relative_path,
                },
            ),
        )
    )


# ---------------------------------------------------------------------------
# Knowledge base helpers
# ---------------------------------------------------------------------------


async def _find_or_create_kb(kb_name: str, user_id: str, db) -> tuple:
    """Look up a KB by *kb_name*; create it if absent.

    Returns ``(knowledge_id, kb_created)`` where *kb_created* is ``True`` when a
    new knowledge base was created during this call.
    """
    _ensure_openwebui_imports()

    existing = None
    lookup_errors = []
    if Knowledge is not None:
        try:
            result = await _db_execute(
                db,
                select(Knowledge).where(Knowledge.name == kb_name).limit(1)
            )
            existing = result.scalars().first()
        except Exception as exc:
            lookup_errors.append(f'orm lookup failed: {exc}')

    if existing is None and hasattr(Knowledges, 'get_knowledge_bases'):
        # Compatibility fallback for builds where the ORM class symbol is not exposed
        # or where the direct ORM query shape changed.
        try:
            kbs = await _call_knowledge_api(
                Knowledges.get_knowledge_bases,
                db=db,
                skip=0,
                limit=2000,
            )
            existing = next(
                (kb for kb in kbs if getattr(kb, 'name', None) == kb_name),
                None,
            )
        except Exception as exc:
            lookup_errors.append(f'knowledge list failed: {exc}')

    if existing:
        return (existing.id, False)

    knowledge_form = (
        KnowledgeForm(
            name=kb_name,
            description='Auto-created by local directory import',
        )
        if KnowledgeForm is not None
        else SimpleNamespace(
            name=kb_name,
            description='Auto-created by local directory import',
        )
    )

    try:
        new_kb = await _call_knowledge_api(
            Knowledges.insert_new_knowledge,
            user_id=user_id,
            id=user_id,
            form=knowledge_form,
            form_data=knowledge_form,
            knowledge_form=knowledge_form,
            data=knowledge_form,
            db=db,
        )
    except Exception as exc:
        lookup_errors.append(f'knowledge create failed: {exc}')
        raise RuntimeError('; '.join(lookup_errors)) from exc

    return (new_kb.id, True)


async def _call_knowledge_api(func, **candidate_values):
    """Call an Open WebUI knowledge helper across signature variations."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        args = []
        kwargs = {}
        missing = []
        for name, param in signature.parameters.items():
            if name in ('self', 'cls'):
                continue
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                continue
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                continue
            if name in candidate_values:
                if param.kind == inspect.Parameter.POSITIONAL_ONLY:
                    args.append(candidate_values[name])
                else:
                    kwargs[name] = candidate_values[name]
                continue
            if param.default is inspect._empty:
                missing.append(name)

        if not missing:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

    fallback_calls = [
        ((candidate_values.get('user_id'), candidate_values.get('form')), {'db': candidate_values.get('db')}),
        ((candidate_values.get('form'),), {'user_id': candidate_values.get('user_id'), 'db': candidate_values.get('db')}),
        ((candidate_values.get('user_id'), candidate_values.get('form')), {}),
        ((), {'skip': candidate_values.get('skip'), 'limit': candidate_values.get('limit'), 'db': candidate_values.get('db')}),
        ((), {'skip': candidate_values.get('skip'), 'limit': candidate_values.get('limit')}),
    ]

    last_error = None
    for args, kwargs in fallback_calls:
        call_args = tuple(arg for arg in args if arg is not None)
        call_kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            result = func(*call_args, **call_kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError('Unable to call knowledge API with supported arguments')


async def _link_file_to_kb(knowledge_id: str, file_id: str, user_id: str, db) -> None:
    """Link an existing file record to a knowledge base."""
    _ensure_openwebui_imports()
    await _maybe_await(
        Knowledges.add_file_to_knowledge_by_id(
            knowledge_id=knowledge_id,
            file_id=file_id,
            user_id=user_id,
            db=db,
        )
    )


# ---------------------------------------------------------------------------
# Vectorization helper
# ---------------------------------------------------------------------------


# File types whose text content must be supplied inline because Open WebUI's
# retrieval pipeline has no native loader for them.
_INLINE_CONTENT_EXTENSIONS = {'.json', '.yml', '.yaml', '.mmd', '.mermaid', '.py', '.ps1', '.html'}


async def _vectorize_file(
    request: Request,
    file_id: str,
    knowledge_id: str,
    user,
    db,
    file_path: pathlib.Path | None = None,
) -> None:
    """Vectorize a file into the KB's collection via the retrieval pipeline.

    For formats without a native Open WebUI loader (JSON, YAML), the file text
    is read here and passed as *content* on the form so the vectorizer does not
    attempt to extract it from disk and return empty content.
    """
    _ensure_openwebui_imports()

    inline_content = None
    if file_path is not None and file_path.suffix.lower() in _INLINE_CONTENT_EXTENSIONS:
        try:
            inline_content = file_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            pass

    if ProcessFileForm is not None:
        form_kwargs = {'file_id': file_id, 'collection_name': knowledge_id}
        if inline_content is not None:
            form_kwargs['content'] = inline_content
        try:
            form = ProcessFileForm(**form_kwargs)
        except TypeError:
            # Older builds may not accept 'content'; fall back without it.
            form = ProcessFileForm(file_id=file_id, collection_name=knowledge_id)
    else:
        form = SimpleNamespace(
            file_id=file_id,
            collection_name=knowledge_id,
            content=inline_content,
        )

    await _maybe_await(
        process_file(
            request,
            form,
            user=user,
            db=db,
        )
    )


@asynccontextmanager
async def _open_db_session():
    """Yield a DB session across Open WebUI dependency shapes."""
    _ensure_openwebui_imports()
    db_provider = get_async_db()

    if inspect.isawaitable(db_provider) and not hasattr(db_provider, '__aenter__'):
        db_provider = await db_provider

    if hasattr(db_provider, '__aenter__') and hasattr(db_provider, '__aexit__'):
        async with db_provider as db:
            yield db
        return

    if hasattr(db_provider, '__enter__') and hasattr(db_provider, '__exit__'):
        with db_provider as db:
            yield db
        return

    if inspect.isasyncgen(db_provider):
        try:
            db = await anext(db_provider)
        except StopAsyncIteration as exc:
            raise RuntimeError('get_async_db yielded no database session') from exc
        try:
            yield db
        finally:
            await db_provider.aclose()
        return

    if inspect.isgenerator(db_provider):
        try:
            db = next(db_provider)
        except StopIteration as exc:
            raise RuntimeError('get_async_db yielded no database session') from exc
        try:
            yield db
        finally:
            db_provider.close()
        return

    yield db_provider


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------


class Tools:
    class Valves(BaseModel):
        drop_folder: str = Field(
            default='/app/backend/data/drop',
            description=(
                'Absolute path to the drop folder to import from. '
                'Each immediate subfolder is mapped to a knowledge base. '
                'Note: local filesystem only — not compatible with S3/GCS/Azure storage backends.'
            ),
        )
        detached_import: bool = Field(
            default=False,
            description=(
                'When true, schedule import work in the background and return '
                'immediately. Progress/errors are written to Open WebUI logs.'
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _run_import_local_directory(
        self,
        __user__: dict,
        __request__: Request,
    ) -> str:
        """Execute the full import pipeline and return JSON summary."""
        overall_start = time.perf_counter()
        drop_folder = self.valves.drop_folder
        if not drop_folder:
            return json.dumps(
                asdict(
                    ImportSummary(
                        error='drop_folder valve is not configured',
                        drop_folder='',
                        total_discovered=0,
                        total_imported=0,
                        total_linked=0,
                        total_processed=0,
                        total_failed=0,
                        knowledge_bases=[],
                    )
                )
            )
        _ensure_openwebui_imports()

        # 1. Admin role guard — must be the first check
        if __user__.get('role') != 'admin':
            return json.dumps(
                asdict(
                    ImportSummary(
                        error='Access denied: admin role required',
                        drop_folder=drop_folder,
                        total_discovered=0,
                        total_imported=0,
                        total_linked=0,
                        total_processed=0,
                        total_failed=0,
                        knowledge_bases=[],
                    )
                )
            )

        # 2. Validate drop_folder exists and is a directory
        drop_path = pathlib.Path(drop_folder).resolve()
        if not drop_path.exists() or not drop_path.is_dir():
            return json.dumps(
                asdict(
                    ImportSummary(
                        error=f"drop_folder '{drop_folder}' does not exist or is not a directory",
                        drop_folder=drop_folder,
                        total_discovered=0,
                        total_imported=0,
                        total_linked=0,
                        total_processed=0,
                        total_failed=0,
                        knowledge_bases=[],
                    )
                )
            )

        user_id = __user__['id']
        user = UserModel(**__user__) if UserModel is not None else __user__
        kb_summaries = []

        # 3. Discover immediate subfolders
        subfolders = _discover_subfolders(drop_path)
        log.info(
            'local_import drop_folder=%s subfolder_count=%d subfolders=%s',
            drop_path,
            len(subfolders),
            [s.name for s in subfolders],
        )

        for subfolder in subfolders:
            kb_name = subfolder.name
            kb_start = time.perf_counter()

            # 4. Find or create the knowledge base for this subfolder
            async with _open_db_session() as db:
                try:
                    knowledge_id, kb_created = await _find_or_create_kb(kb_name, user_id, db)
                except Exception as exc:
                    log.error(
                        'local_import kb_find_or_create kb=%s error=%s',
                        kb_name,
                        str(exc),
                    )
                    kb_summaries.append(
                        KBImportSummary(
                            kb_name=kb_name,
                            knowledge_id=None,
                            kb_created=False,
                            discovered=0,
                            imported=0,
                            linked=0,
                            processed=0,
                            failed=1,
                            files=[],
                            error=str(exc),
                            duration_seconds=round(time.perf_counter() - kb_start, 3),
                            files_per_second=0.0,
                        )
                    )
                    continue

                kb_summary = KBImportSummary(
                    kb_name=kb_name,
                    knowledge_id=knowledge_id,
                    kb_created=kb_created,
                    discovered=0,
                    imported=0,
                    linked=0,
                    processed=0,
                    failed=0,
                    skipped=0,
                    files=[],
                )

                # 5. Discover and process files within this subfolder
                files = _discover_files(subfolder)
                kb_summary.discovered = len(files)
                log.info(
                    'local_import kb=%s knowledge_id=%s kb_created=%s discovered=%d',
                    kb_name,
                    knowledge_id,
                    kb_created,
                    len(files),
                )

                for file_idx, file_path in enumerate(files, 1):
                    file_id = str(uuid.uuid4())
                    filename = file_path.name
                    relative_path = str(file_path.relative_to(subfolder))
                    status = 'discovered'
                    error = None
                    log.info(
                        'local_import processing kb=%s file=%d/%d path=%s',
                        kb_name,
                        file_idx,
                        len(files),
                        relative_path,
                    )

                    # Hash check — skip files that haven't changed
                    try:
                        file_hash = _hash_file(file_path)
                        existing = await _find_file_by_hash(file_hash, db)
                        if existing is not None:
                            kb_summary.skipped += 1
                            log.info(
                                'local_import file=%s kb=%s status=skipped hash=%s',
                                relative_path,
                                kb_name,
                                file_hash,
                            )
                            kb_summary.files.append(
                                ImportFileResult(
                                    relative_path=relative_path,
                                    filename=filename,
                                    file_id=existing.id,
                                    status='skipped',
                                )
                            )
                            continue
                    except Exception as exc:
                        error = str(exc)
                        status = 'hash_failed'
                        kb_summary.failed += 1
                        log.error(
                            'local_import file=%s kb=%s status=%s reason=%s',
                            relative_path,
                            kb_name,
                            status,
                            error,
                        )
                        kb_summary.files.append(
                            ImportFileResult(
                                relative_path=relative_path,
                                filename=filename,
                                file_id=None,
                                status=status,
                                error=error,
                            )
                        )
                        continue

                    # Copy + insert file record
                    try:
                        dest = _copy_file_to_upload_dir(file_path, file_id, filename)
                        await _insert_file_record(
                            user_id, file_id, filename, dest, relative_path, file_hash
                        )
                        kb_summary.imported += 1
                        status = 'imported'
                    except Exception as exc:
                        error = str(exc)
                        status = 'import_failed'
                        kb_summary.failed += 1
                        log.info(
                            'local_import file=%s kb=%s status=%s reason=%s',
                            relative_path,
                            kb_name,
                            status,
                            error,
                        )
                        kb_summary.files.append(
                            ImportFileResult(
                                relative_path=relative_path,
                                filename=filename,
                                file_id=None,
                                status=status,
                                error=error,
                            )
                        )
                        continue

                    # Link file to KB
                    try:
                        await _link_file_to_kb(knowledge_id, file_id, user_id, db)
                        kb_summary.linked += 1
                        status = 'linked'
                    except Exception as exc:
                        error = str(exc)
                        status = 'import_failed'
                        kb_summary.failed += 1
                        log.info(
                            'local_import file=%s kb=%s status=%s reason=%s',
                            relative_path,
                            kb_name,
                            status,
                            error,
                        )
                        kb_summary.files.append(
                            ImportFileResult(
                                relative_path=relative_path,
                                filename=filename,
                                file_id=file_id,
                                status=status,
                                error=error,
                            )
                        )
                        continue

                    # Vectorize — failures are non-fatal (FR-017)
                    try:
                        await _vectorize_file(
                            __request__, file_id, knowledge_id, user, db,
                            file_path=file_path,
                        )
                        kb_summary.processed += 1
                        status = 'processed'
                    except Exception as exc:
                        error = str(exc)
                        status = 'vectorization_failed'
                        kb_summary.failed += 1

                    log.info(
                        'local_import file=%s kb=%s status=%s reason=%s',
                        relative_path,
                        kb_name,
                        status,
                        error or '',
                    )
                    kb_summary.files.append(
                        ImportFileResult(
                            relative_path=relative_path,
                            filename=filename,
                            file_id=file_id,
                            status=status,
                            error=error,
                        )
                    )

                kb_summary.duration_seconds = round(time.perf_counter() - kb_start, 3)
                if kb_summary.duration_seconds > 0:
                    kb_summary.files_per_second = round(
                        kb_summary.discovered / kb_summary.duration_seconds,
                        3,
                    )

            kb_summaries.append(kb_summary)

        # 6. Aggregate totals
        total_discovered = sum(kb.discovered for kb in kb_summaries)
        duration_seconds = round(time.perf_counter() - overall_start, 3)
        files_per_second = (
            round(total_discovered / duration_seconds, 3)
            if duration_seconds > 0
            else 0.0
        )

        summary = ImportSummary(
            drop_folder=drop_folder,
            total_discovered=total_discovered,
            total_imported=sum(kb.imported for kb in kb_summaries),
            total_linked=sum(kb.linked for kb in kb_summaries),
            total_processed=sum(kb.processed for kb in kb_summaries),
            total_failed=sum(kb.failed for kb in kb_summaries),
            total_skipped=sum(kb.skipped for kb in kb_summaries),
            knowledge_bases=kb_summaries,
            duration_seconds=duration_seconds,
            files_per_second=files_per_second,
            error=(
                'One or more knowledge bases failed to import; '
                'see knowledge_bases[*].error'
                if any(kb.error for kb in kb_summaries)
                else None
            ),
        )

        log.info(
            'local_import summary drop_folder=%s total_discovered=%d '
            'total_imported=%d total_linked=%d total_processed=%d total_failed=%d',
            drop_folder,
            summary.total_discovered,
            summary.total_imported,
            summary.total_linked,
            summary.total_processed,
            summary.total_failed,
        )

        return json.dumps(asdict(summary))

    async def _run_import_local_directory_detached(
        self,
        __user__: dict,
        __request__: Request,
    ) -> None:
        """Execute import in a background task and log outcome."""
        try:
            result = await self._run_import_local_directory(__user__, __request__)
            log.info('local_import detached_completed summary=%s', result)
        except Exception:
            log.exception('local_import detached_failed')

    async def import_local_directory(
        self,
        __user__: dict = {},
        __request__: Request = None,
    ) -> str:
        """
        Import all files from the configured drop folder into knowledge bases.

        The drop folder path is set by the admin in the Valves configuration
        (drop_folder). Each immediate subfolder is mapped to a knowledge
        base with the same name (created automatically if it does not exist).
        All files within each subfolder (recursively) are copied to the upload
        directory, registered in the database, linked to the KB, and vectorized.

        :return: JSON string containing an ImportSummary with per-KB breakdowns.
        """
        if getattr(self.valves, 'detached_import', False) is True:
            # Copy user payload to avoid accidental mutation after dispatch.
            user_copy = dict(__user__) if isinstance(__user__, dict) else __user__
            asyncio.create_task(
                self._run_import_local_directory_detached(user_copy, __request__)
            )
            return json.dumps({'status': 'dispatched'})

        return await self._run_import_local_directory(__user__, __request__)
