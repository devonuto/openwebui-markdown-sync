#!/usr/bin/env python3
"""Standalone import trigger for the local_directory_import Open WebUI tool.

Runs inside the container via docker exec — bypasses the LLM entirely so tool
execution is guaranteed regardless of Open WebUI version or provider settings.

Usage:
    docker exec open-webui python3 /scripts/run_import.py [drop_folder]

The drop_folder argument defaults to /app/backend/data/drop.
"""

import asyncio
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


async def main() -> None:
    # ── 1. Find the first admin user ─────────────────────────────────────────
    # Import only the users model to avoid triggering the full app startup.
    try:
        from open_webui.models.users import Users  # type: ignore
    except ImportError as exc:
        sys.exit(f'ERROR: cannot import open_webui.models.users: {exc}')

    try:
        all_users = await _maybe_await(Users.get_users()) or []
    except Exception as exc:
        sys.exit(f'ERROR: Users.get_users() failed: {exc}')

    admin = next(
        (u for u in all_users if getattr(u, 'role', '') == 'admin'),
        None,
    )
    if admin is None:
        sys.exit('ERROR: no admin user found in the database')

    user_dict = {
        'id': admin.id,
        'email': getattr(admin, 'email', ''),
        'name': getattr(admin, 'name', 'admin'),
        'role': 'admin',
    }
    log.info('run_import admin_id=%s email=%s', admin.id, user_dict['email'])

    # ── 2. Build a Starlette Request backed by the real app state ─────────────
    # process_file() (vectorization) reads request.app.state for RAG config.
    # Importing open_webui.main in a new process is heavy but avoids a mocked
    # state that would cause vectorization to silently fail.
    try:
        from open_webui.main import app  # type: ignore
        from starlette.requests import Request  # type: ignore

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

    tool_instance = ToolClass()
    tool_instance.valves.drop_folder = DROP_FOLDER
    log.info('run_import drop_folder=%s', DROP_FOLDER)

    # ── 5. Run the import ────────────────────────────────────────────────────
    result_json = await tool_instance._run_import_local_directory(
        user_dict, mock_request
    )
    print(result_json)


asyncio.run(main())
