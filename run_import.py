#!/usr/bin/env python3
"""Standalone import trigger for the local_directory_import Open WebUI tool.

Runs inside the container via docker exec — bypasses the LLM entirely so tool
execution is guaranteed regardless of Open WebUI version or provider settings.

Usage:
    docker exec open-webui python3 /scripts/run_import.py [drop_folder]

The drop_folder argument defaults to /app/backend/data/drop.
"""

import asyncio
import collections.abc
import datetime as dt
import inspect
import logging
import sys

# Open WebUI's backend must be on the path.
sys.path.insert(0, '/app/backend')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('run_import')

DROP_FOLDER = sys.argv[1] if len(sys.argv) > 1 else '/app/backend/data/drop'


async def _maybe_await(value):
    """Await value when it is awaitable; otherwise return as-is."""
    if inspect.isawaitable(value):
        return await value
    return value


def _get_field(obj, key, default=None):
    """Read field from dict-like or attribute-style objects."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_items(value):
    """Normalize mixed API return shapes to a list of items."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        # Some APIs return (items, total)
        if value and isinstance(value[0], list):
            return value[0]
        return [v for v in value if v is not None]
    if isinstance(value, dict):
        # Common wrapper shapes: {"users": [...]}, {"data": [...]}
        for key in ('users', 'data', 'items', 'results'):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
        return [value]
    return [value]


async def _collect_users(users_api):
    """Collect users from multiple possible API shapes/method names."""
    users = []
    getter_names = ('get_users', 'get_all_users', 'list_users')
    for name in getter_names:
        getter = getattr(users_api, name, None)
        if getter is None:
            continue
        try:
            value = getter()
            value = await _maybe_await(value)
            if inspect.isasyncgen(value):
                async for item in value:
                    users.extend(_normalize_items(item))
            elif isinstance(value, collections.abc.Generator):
                for item in value:
                    users.extend(_normalize_items(item))
            else:
                users.extend(_normalize_items(value))
        except Exception as exc:
            log.warning('run_import users getter %s failed: %s', name, exc)
    if users:
        return users

    # Last resort: single-user getter.
    first_getter = getattr(users_api, 'get_first_user', None)
    if first_getter is not None:
        try:
            first = await _maybe_await(first_getter())
            return _normalize_items(first)
        except Exception as exc:
            log.warning('run_import users getter get_first_user failed: %s', exc)

    return []


def _is_admin_user(user) -> bool:
    """Return True when user appears to have admin privileges."""
    role = _get_field(user, 'role', '')
    role_texts = [str(role)]
    # Handle enum-like roles.
    role_value = getattr(role, 'value', None)
    role_name = getattr(role, 'name', None)
    if role_value is not None:
        role_texts.append(str(role_value))
    if role_name is not None:
        role_texts.append(str(role_name))

    if any(text.strip().lower() == 'admin' for text in role_texts):
        return True

    if bool(_get_field(user, 'is_admin', False)):
        return True

    return False


def _build_user_payload(user) -> dict:
    """Build a dict payload compatible with Open WebUI UserModel validation."""
    payload = {}

    if isinstance(user, dict):
        payload = dict(user)
    elif hasattr(user, 'model_dump'):
        try:
            payload = user.model_dump()  # Pydantic v2 models
        except Exception:
            payload = {}
    elif hasattr(user, 'dict'):
        try:
            payload = user.dict()  # Pydantic v1 models
        except Exception:
            payload = {}
    else:
        try:
            payload = {
                k: v for k, v in vars(user).items()
                if not k.startswith('_')
            }
        except Exception:
            payload = {}

    # Ensure core fields exist and are aligned with admin account.
    payload['id'] = _get_field(user, 'id', payload.get('id'))
    payload['email'] = _get_field(user, 'email', payload.get('email', ''))
    payload['name'] = _get_field(user, 'name', payload.get('name', 'admin'))
    payload['role'] = 'admin'

    # Open WebUI UserModel in some versions requires these datetime fields.
    now = dt.datetime.now(dt.timezone.utc)
    if payload.get('created_at') is None:
        payload['created_at'] = _get_field(user, 'created_at', now)
    if payload.get('updated_at') is None:
        payload['updated_at'] = _get_field(user, 'updated_at', now)
    if payload.get('last_active_at') is None:
        payload['last_active_at'] = _get_field(user, 'last_active_at', now)

    return payload


async def main() -> None:
    # ── 1. Find the first admin user ─────────────────────────────────────────
    # Import only the users model to avoid triggering the full app startup.
    try:
        from open_webui.models.users import Users  # type: ignore
    except ImportError as exc:
        sys.exit(f'ERROR: cannot import open_webui.models.users: {exc}')

    try:
        all_users = await _collect_users(Users)
    except Exception as exc:
        sys.exit(f'ERROR: users lookup failed: {exc}')

    log.info('run_import users_found=%d', len(all_users))
    if all_users:
        preview = [
            {
                'id': _get_field(u, 'id', '?'),
                'email': _get_field(u, 'email', ''),
                'role': str(_get_field(u, 'role', '')),
                'is_admin': bool(_get_field(u, 'is_admin', False)),
            }
            for u in all_users[:5]
        ]
        log.info('run_import users_preview=%s', preview)

    admin = next(
        (u for u in all_users if _is_admin_user(u)),
        None,
    )
    if admin is None:
        sys.exit('ERROR: no admin user found in the database')

    user_dict = _build_user_payload(admin)
    if not user_dict['id']:
        sys.exit('ERROR: admin user resolved but id is missing')
    log.info('run_import admin_id=%s email=%s', user_dict['id'], user_dict['email'])

    # ── 2. Build a Starlette Request backed by the real app state ─────────────
    # process_file() (vectorization) reads request.app.state for RAG config.
    # Importing open_webui.main in a new process is heavy but avoids a mocked
    # state that would cause vectorization to silently fail.
    try:
        from open_webui.main import app  # type: ignore
        from starlette.requests import Request  # type: ignore

        # Direct script execution bypasses ASGI startup events in some builds,
        # so initialize main_loop if retrieval expects it.
        if not hasattr(app.state, 'main_loop'):
            app.state.main_loop = asyncio.get_running_loop()
            log.info('run_import app.state.main_loop initialized')

        scope = {
            'type': 'http',
            'method': 'POST',
            'path': '/internal/run_import',
            'query_string': b'',
            'headers': [],
            'app': app,
        }
        mock_request = Request(scope)
        log.info('run_import app state loaded OK')
    except Exception as exc:
        log.warning(
            'run_import could not load app state (%s) — '
            'vectorization will likely fail (non-fatal)',
            exc,
        )
        mock_request = None

    # ── 3. Load the tool code from Open WebUI's database ────────────────────
    # Tools are stored as source code in the DB; exec() them to get the class.
    try:
        from open_webui.models.tools import Tools as DBTools  # type: ignore

        tool_record = None
        for candidate_id in ('local_directory_import',):
            try:
                tool_record = await _maybe_await(DBTools.get_tool_by_id(candidate_id))
            except Exception:
                pass
            if tool_record is not None:
                break

        if tool_record is None:
            # Fallback: search all tools for one whose id or name matches.
            all_tools = await _maybe_await(DBTools.get_tools()) or []
            tool_record = next(
                (
                    t for t in all_tools
                    if getattr(t, 'id', '') == 'local_directory_import'
                    or 'local_directory_import' in getattr(t, 'name', '').lower()
                ),
                None,
            )

        if tool_record is None:
            available_tools = await _maybe_await(DBTools.get_tools()) or []
            log.error(
                'run_import available tool ids=%s',
                [getattr(t, 'id', '?') for t in available_tools],
            )
            sys.exit('ERROR: tool local_directory_import not found in the DB')

        log.info(
            'run_import found tool id=%s name=%s',
            getattr(tool_record, 'id', '?'),
            getattr(tool_record, 'name', '?'),
        )

    except Exception as exc:
        sys.exit(f'ERROR: cannot load tool from DB: {exc}')

    # ── 4. Exec the tool code and instantiate Tools ──────────────────────────
    module_ns: dict = {}
    try:
        exec(tool_record.content, module_ns)  # noqa: S102
    except Exception as exc:
        sys.exit(f'ERROR: exec of tool content failed: {exc}')

    ToolClass = module_ns.get('Tools')
    if ToolClass is None:
        sys.exit('ERROR: no Tools class found after exec of tool content')

    # Reduce noisy retrieval errors when running as a scheduler job:
    # - skip vectorization for image files
    # - treat EMPTY_CONTENT vectorization errors as non-fatal
    original_vectorize = module_ns.get('_vectorize_file')
    if callable(original_vectorize):
        skip_exts = {'.png', '.jpg', '.jpeg', '.svg'}

        async def _vectorize_file_safe(
            request,
            file_id,
            knowledge_id,
            user,
            db,
            file_path=None,
        ):
            suffix = ''
            if file_path is not None:
                suffix = str(getattr(file_path, 'suffix', '')).lower()

            if suffix in skip_exts:
                log.info(
                    'run_import vectorize_skip file_id=%s reason=non_text_extension ext=%s',
                    file_id,
                    suffix,
                )
                return None

            try:
                result = original_vectorize(
                    request,
                    file_id,
                    knowledge_id,
                    user,
                    db,
                    file_path=file_path,
                )
                return await _maybe_await(result)
            except Exception as exc:
                if 'content provided is empty' in str(exc).lower():
                    log.info(
                        'run_import vectorize_skip file_id=%s reason=empty_content',
                        file_id,
                    )
                    return None
                raise

        module_ns['_vectorize_file'] = _vectorize_file_safe
        log.info('run_import vectorize monkeypatch enabled')

    tool_instance = ToolClass()
    tool_instance.valves.drop_folder = DROP_FOLDER
    log.info('run_import drop_folder=%s', DROP_FOLDER)

    # ── 5. Run the import ────────────────────────────────────────────────────
    result_json = await tool_instance._run_import_local_directory(
        user_dict, mock_request
    )
    print(result_json)


asyncio.run(main())
