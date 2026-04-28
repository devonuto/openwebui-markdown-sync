"""
Local Directory Import Plugin for Open WebUI.

Bulk-imports files from a local drop folder into knowledge bases.
Each immediate subfolder of the drop folder is auto-mapped to a knowledge base
named after it (created if it does not exist). Files are copied to UPLOAD_DIR,
registered in the database, linked to the corresponding KB, and vectorized.

Admin-only access. Allow-list path security via Valves.
Returns a JSON summary with per-KB breakdowns.

Note: local filesystem only — not compatible with S3/GCS/Azure storage backends.
"""

__version__ = '0.1.0'

import hashlib
import importlib
import json
import logging
import mimetypes
import pathlib
import shutil
import sys
import uuid
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


async def _find_file_by_hash(file_hash: str, db) -> 'File | None':
    """Return the first File record whose hash matches *file_hash*, or None."""
    _ensure_openwebui_imports()
    result = await db.execute(
        select(File).where(File.hash == file_hash).limit(1)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_subfolders(drop_folder: pathlib.Path) -> list:
    """Return a sorted list of immediate subdirectories inside *drop_folder*."""
    return sorted([p for p in drop_folder.iterdir() if p.is_dir()])


def _discover_files(subfolder: pathlib.Path) -> list:
    """Return a sorted list of all files recursively inside *subfolder*."""
    return sorted([p for p in subfolder.rglob('*') if p.is_file()])


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
    await Files.insert_new_file(
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
    if Knowledge is not None:
        result = await db.execute(
            select(Knowledge).where(Knowledge.name == kb_name).limit(1)
        )
        existing = result.scalars().first()
    elif hasattr(Knowledges, 'get_knowledge_bases'):
        # Compatibility fallback for builds where the ORM class symbol is not exposed.
        kbs = await Knowledges.get_knowledge_bases(skip=0, limit=2000, db=db)
        existing = next((kb for kb in kbs if getattr(kb, 'name', None) == kb_name), None)

    if existing:
        return (existing.id, False)

    new_kb = await Knowledges.insert_new_knowledge(
        user_id,
        KnowledgeForm(
            name=kb_name,
            description='Auto-created by local directory import',
        ),
        db=db,
    )
    return (new_kb.id, True)


async def _link_file_to_kb(knowledge_id: str, file_id: str, user_id: str, db) -> None:
    """Link an existing file record to a knowledge base."""
    _ensure_openwebui_imports()
    await Knowledges.add_file_to_knowledge_by_id(
        knowledge_id=knowledge_id,
        file_id=file_id,
        user_id=user_id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Vectorization helper
# ---------------------------------------------------------------------------


async def _vectorize_file(
    request: Request,
    file_id: str,
    knowledge_id: str,
    user,
    db,
) -> None:
    """Vectorize a file into the KB's collection via the retrieval pipeline."""
    _ensure_openwebui_imports()
    form = (
        ProcessFileForm(file_id=file_id, collection_name=knowledge_id)
        if ProcessFileForm is not None
        else SimpleNamespace(file_id=file_id, collection_name=knowledge_id, content=None)
    )
    await process_file(
        request,
        form,
        user=user,
        db=db,
    )


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

    def __init__(self):
        self.valves = self.Valves()

    async def import_local_directory(
        self,
        __user__: dict = {},
        __request__: Request = None,
    ) -> str:
        """
        Import all files from the configured drop folder into knowledge bases.

        The drop folder path is set by the admin in the Valves configuration
        (allowed_base_dirs). Each immediate subfolder is mapped to a knowledge
        base with the same name (created automatically if it does not exist).
        All files within each subfolder (recursively) are copied to the upload
        directory, registered in the database, linked to the KB, and vectorized.

        :return: JSON string containing an ImportSummary with per-KB breakdowns.
        """
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

        for subfolder in subfolders:
            kb_name = subfolder.name

            # 4. Find or create the knowledge base for this subfolder
            async with get_async_db() as db:
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
                            failed=0,
                            files=[],
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

                for file_path in files:
                    file_id = str(uuid.uuid4())
                    filename = file_path.name
                    relative_path = str(file_path.relative_to(subfolder))
                    status = 'discovered'
                    error = None

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
                            __request__, file_id, knowledge_id, user, db
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

            kb_summaries.append(kb_summary)

        # 6. Aggregate totals
        summary = ImportSummary(
            drop_folder=drop_folder,
            total_discovered=sum(kb.discovered for kb in kb_summaries),
            total_imported=sum(kb.imported for kb in kb_summaries),
            total_linked=sum(kb.linked for kb in kb_summaries),
            total_processed=sum(kb.processed for kb in kb_summaries),
            total_failed=sum(kb.failed for kb in kb_summaries),
            total_skipped=sum(kb.skipped for kb in kb_summaries),
            knowledge_bases=kb_summaries,
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
