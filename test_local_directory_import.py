"""Unit tests for the local_directory_import Tool plugin."""

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the module under test without needing a live DB/config
# ---------------------------------------------------------------------------


def _make_import_module():
    """Import the plugin module with open_webui.* dependencies mocked."""
    import sys

    mocks = {
        'fastapi': MagicMock(),
        'pydantic': MagicMock(),
        'sqlalchemy': MagicMock(),
        'open_webui': MagicMock(),
        'open_webui.config': MagicMock(UPLOAD_DIR='/tmp/uploads'),
        'open_webui.internal': MagicMock(),
        'open_webui.internal.db': MagicMock(),
        'open_webui.models': MagicMock(),
        'open_webui.models.files': MagicMock(),
        'open_webui.models.knowledge': MagicMock(),
        'open_webui.models.users': MagicMock(),
        'open_webui.routers': MagicMock(),
        'open_webui.routers.retrieval': MagicMock(),
    }

    # Install mocks only for modules not yet in sys.modules
    installed = {}
    for key, mock in mocks.items():
        if key not in sys.modules:
            sys.modules[key] = mock
            installed[key] = mock

    # Force a fresh import of the plugin
    plugin_key = 'local_directory_import'
    if plugin_key in sys.modules:
        del sys.modules[plugin_key]

    import local_directory_import as plugin  # noqa: E402

    return plugin, installed


# Load the module once for the entire test session (always via mock shim,
# since fastapi/sqlalchemy/open_webui are not installed in the test environment)
_plugin_module, _ = _make_import_module()


# Re-export symbols for brevity
_resolve_allowed_base = _plugin_module._resolve_allowed_base
_discover_subfolders = _plugin_module._discover_subfolders
_discover_files = _plugin_module._discover_files
_find_or_create_kb = _plugin_module._find_or_create_kb
ImportSummary = _plugin_module.ImportSummary
KBImportSummary = _plugin_module.KBImportSummary
ImportFileResult = _plugin_module.ImportFileResult
Tools = _plugin_module.Tools


# ---------------------------------------------------------------------------
# T012 — _discover_subfolders & _discover_files
# ---------------------------------------------------------------------------


class TestDiscoverSubfolders:
    def test_returns_immediate_subdirs(self, tmp_path):
        """(a) Drop folder with two subfolders returns both."""
        (tmp_path / 'alpha').mkdir()
        (tmp_path / 'beta').mkdir()
        result = _discover_subfolders(tmp_path)
        assert [p.name for p in result] == ['alpha', 'beta']

    def test_ignores_files_at_root(self, tmp_path):
        """(b) Drop folder with files at root but no subfolders returns empty list."""
        (tmp_path / 'readme.txt').write_text('hello')
        result = _discover_subfolders(tmp_path)
        assert result == []

    def test_empty_drop_folder(self, tmp_path):
        """(c) Empty drop folder returns empty list."""
        assert _discover_subfolders(tmp_path) == []


class TestDiscoverFiles:
    def test_flat_subfolder(self, tmp_path):
        """(d) Flat subfolder returns all files."""
        (tmp_path / 'a.txt').write_text('a')
        (tmp_path / 'b.md').write_text('b')
        result = _discover_files(tmp_path)
        assert sorted(p.name for p in result) == ['a.txt', 'b.md']

    def test_nested_subdirectories(self, tmp_path):
        """(e) Nested subdirectories returns all files recursively."""
        sub = tmp_path / 'sub'
        sub.mkdir()
        (tmp_path / 'root.txt').write_text('r')
        (sub / 'nested.md').write_text('n')
        result = _discover_files(tmp_path)
        names = sorted(p.name for p in result)
        assert 'root.txt' in names
        assert 'nested.md' in names

    def test_empty_subfolder(self, tmp_path):
        """(f) Empty subfolder returns empty list."""
        assert _discover_files(tmp_path) == []


# ---------------------------------------------------------------------------
# T013 — _resolve_allowed_base & _find_or_create_kb
# ---------------------------------------------------------------------------


class TestResolveAllowedBase:
    def test_path_inside_allowed_dir(self, tmp_path):
        """(a) Path inside allowed dir returns base."""
        base = tmp_path / 'allowed'
        base.mkdir()
        target = base / 'subdir'
        target.mkdir()
        result = _resolve_allowed_base(str(target), [str(base)])
        assert result == base.resolve()

    def test_path_outside_all_allowed_dirs(self, tmp_path):
        """(b) Path outside all allowed dirs raises ValueError."""
        allowed = tmp_path / 'allowed'
        allowed.mkdir()
        other = tmp_path / 'other'
        other.mkdir()
        with pytest.raises(ValueError, match='not within any permitted base directory'):
            _resolve_allowed_base(str(other), [str(allowed)])

    def test_empty_allow_list_raises(self, tmp_path):
        """(c) Empty allow-list raises ValueError."""
        with pytest.raises(ValueError):
            _resolve_allowed_base(str(tmp_path), [])

    def test_symlink_traversal_blocked(self, tmp_path):
        """(d) Symlink traversal attempt (path resolves outside base) raises ValueError."""
        base = tmp_path / 'base'
        base.mkdir()
        outside = tmp_path / 'outside'
        outside.mkdir()
        link = base / 'link'
        link.symlink_to(outside)
        with pytest.raises(ValueError):
            _resolve_allowed_base(str(link), [str(base)])


class TestFindOrCreateKb:
    @pytest.mark.asyncio
    async def test_existing_kb_returns_id_false(self):
        """(e) Existing KB by name returns (id, False)."""
        fake_kb = MagicMock()
        fake_kb.id = 'existing-id'

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = fake_kb

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        result = await _find_or_create_kb('my-kb', 'user-1', mock_db)
        assert result == ('existing-id', False)

    @pytest.mark.asyncio
    async def test_missing_kb_creates_and_returns_true(self):
        """(f) No matching KB creates new KB and returns (new_id, True)."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        new_kb = MagicMock()
        new_kb.id = 'new-id'

        with patch.object(_plugin_module.Knowledges, 'insert_new_knowledge', new=AsyncMock(return_value=new_kb)):
            result = await _find_or_create_kb('new-kb', 'user-1', mock_db)

        assert result == ('new-id', True)


# ---------------------------------------------------------------------------
# T014 — import_local_directory happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_local_directory_happy_path(tmp_path):
    """
    Integration-style unit test: two subfolders with nested files.
    First subfolder maps to an existing KB; second to a new KB.
    """
    # Build temp directory tree
    pp = tmp_path / 'power-platform'
    pp.mkdir()
    (pp / 'readme.md').write_text('pp readme')
    (pp / 'docs').mkdir()
    (pp / 'docs' / 'guide.md').write_text('pp guide')

    af = tmp_path / 'azure-functions'
    af.mkdir()
    (af / 'main.py').write_text("print('hello')")

    admin_user = {
        'id': 'admin-1',
        'email': 'admin@example.com',
        'name': 'Admin',
        'role': 'admin',
    }
    mock_request = MagicMock()

    # Mock Knowledges.add_file_to_knowledge_by_id
    with (
        patch.object(
            _plugin_module.Knowledges,
            'add_file_to_knowledge_by_id',
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch.object(
            _plugin_module.Files,
            'insert_new_file',
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch.object(_plugin_module, 'process_file', new=AsyncMock(return_value=None)),
        patch('shutil.copy'),
        patch.object(_plugin_module, '_find_or_create_kb') as mock_fock,
        patch.object(pathlib.Path, 'stat') as mock_stat,
        patch.object(_plugin_module, 'get_async_db'),
    ):
        # First subfolder (azure-functions) → existing KB, second (power-platform) → new KB
        mock_fock.side_effect = [
            ('kb-az', False),   # azure-functions (sorted first)
            ('kb-pp', True),    # power-platform
        ]
        mock_stat.return_value = MagicMock(st_size=100)

        # Patch get_async_db to be an async context manager
        async_ctx = MagicMock()
        async_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        async_ctx.__aexit__ = AsyncMock(return_value=False)
        _plugin_module.get_async_db.return_value = async_ctx

        tools = Tools()
        tools.valves.allowed_base_dirs = [str(tmp_path)]

        result_str = await tools.import_local_directory(
            str(tmp_path), admin_user, mock_request
        )

    data = json.loads(result_str)

    assert 'error' not in data or data['error'] is None
    assert data['total_discovered'] == 3  # 2 + 1
    assert len(data['knowledge_bases']) == 2

    # kb_created flags
    kb_map = {kb['kb_name']: kb for kb in data['knowledge_bases']}
    assert kb_map['azure-functions']['kb_created'] is False
    assert kb_map['power-platform']['kb_created'] is True

    # relative_path is relative to subfolder root
    pp_files = kb_map['power-platform']['files']
    pp_rel_paths = {f['relative_path'] for f in pp_files}
    assert 'readme.md' in pp_rel_paths
    # docs/guide.md uses the platform path separator
    assert any('guide.md' in rp for rp in pp_rel_paths)


# ---------------------------------------------------------------------------
# T016 — Access control
# ---------------------------------------------------------------------------


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_non_admin_returns_error_summary(self):
        """(a) Non-admin role returns error summary with all counts 0."""
        tools = Tools()
        tools.valves.allowed_base_dirs = ['/tmp']
        user = {'id': 'u1', 'role': 'user', 'email': 'u@x.com', 'name': 'U'}

        with patch('shutil.copy') as mock_copy:
            result_str = await tools.import_local_directory(
                '/tmp/anything', user, MagicMock()
            )
            mock_copy.assert_not_called()

        data = json.loads(result_str)
        assert data['error'] == 'Access denied: admin role required'
        assert data['total_discovered'] == 0
        assert data['total_imported'] == 0

    @pytest.mark.asyncio
    async def test_path_outside_allowlist_returns_error(self, tmp_path):
        """(b) Path outside allow-list returns error summary with all counts 0."""
        tools = Tools()
        allowed = tmp_path / 'allowed'
        allowed.mkdir()
        other = tmp_path / 'other'
        other.mkdir()
        tools.valves.allowed_base_dirs = [str(allowed)]

        admin_user = {'id': 'a1', 'role': 'admin', 'email': 'a@x.com', 'name': 'A'}

        with patch('shutil.copy') as mock_copy:
            result_str = await tools.import_local_directory(
                str(other), admin_user, MagicMock()
            )
            mock_copy.assert_not_called()

        data = json.loads(result_str)
        assert data['error'] is not None
        assert data['total_discovered'] == 0

    @pytest.mark.asyncio
    async def test_nonexistent_drop_folder_returns_error(self, tmp_path):
        """(c) Non-existent drop folder returns error before file discovery."""
        tools = Tools()
        tools.valves.allowed_base_dirs = [str(tmp_path)]
        admin_user = {'id': 'a1', 'role': 'admin', 'email': 'a@x.com', 'name': 'A'}
        nonexistent = str(tmp_path / 'does_not_exist')

        with patch.object(_plugin_module, '_discover_subfolders') as mock_disc:
            result_str = await tools.import_local_directory(
                nonexistent, admin_user, MagicMock()
            )
            mock_disc.assert_not_called()

        data = json.loads(result_str)
        assert data['error'] is not None
        assert data['total_discovered'] == 0


# ---------------------------------------------------------------------------
# T020 — Vectorization
# ---------------------------------------------------------------------------


class TestVectorization:
    @pytest.mark.asyncio
    async def test_successful_vectorization_increments_processed(self, tmp_path):
        """(a) Successful vectorization increments processed count."""
        sub = tmp_path / 'kb1'
        sub.mkdir()
        (sub / 'file.txt').write_text('content')

        admin_user = {'id': 'a1', 'role': 'admin', 'email': 'a@x.com', 'name': 'A'}

        with (
            patch.object(
                _plugin_module.Files,
                'insert_new_file',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(
                _plugin_module.Knowledges,
                'add_file_to_knowledge_by_id',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(_plugin_module, 'process_file', new=AsyncMock(return_value=None)) as mock_proc,
            patch('shutil.copy'),
            patch.object(_plugin_module, '_find_or_create_kb', new=AsyncMock(return_value=('kb-id', False))),
            patch.object(pathlib.Path, 'stat', return_value=MagicMock(st_size=10)),
            patch.object(_plugin_module, 'get_async_db'),
        ):
            async_ctx = MagicMock()
            async_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
            async_ctx.__aexit__ = AsyncMock(return_value=False)
            _plugin_module.get_async_db.return_value = async_ctx

            tools = Tools()
            tools.valves.allowed_base_dirs = [str(tmp_path)]
            result_str = await tools.import_local_directory(str(tmp_path), admin_user, MagicMock())

        data = json.loads(result_str)
        assert data['total_processed'] == 1
        assert data['total_failed'] == 0
        mock_proc.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_vectorization_failure_marks_file_and_retains_record(self, tmp_path):
        """(b) Vectorization exception: status=vectorization_failed, file record kept, failed incremented."""
        sub = tmp_path / 'kb1'
        sub.mkdir()
        (sub / 'file.txt').write_text('content')

        admin_user = {'id': 'a1', 'role': 'admin', 'email': 'a@x.com', 'name': 'A'}
        mock_insert = AsyncMock(return_value=MagicMock())

        with (
            patch.object(_plugin_module.Files, 'insert_new_file', new=mock_insert),
            patch.object(
                _plugin_module.Knowledges,
                'add_file_to_knowledge_by_id',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(
                _plugin_module,
                'process_file',
                new=AsyncMock(side_effect=RuntimeError('embedding failed')),
            ),
            patch('shutil.copy'),
            patch.object(_plugin_module, '_find_or_create_kb', new=AsyncMock(return_value=('kb-id', False))),
            patch.object(pathlib.Path, 'stat', return_value=MagicMock(st_size=10)),
            patch.object(_plugin_module, 'get_async_db'),
        ):
            async_ctx = MagicMock()
            async_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
            async_ctx.__aexit__ = AsyncMock(return_value=False)
            _plugin_module.get_async_db.return_value = async_ctx

            tools = Tools()
            tools.valves.allowed_base_dirs = [str(tmp_path)]
            result_str = await tools.import_local_directory(str(tmp_path), admin_user, MagicMock())

        data = json.loads(result_str)
        kb = data['knowledge_bases'][0]
        assert kb['failed'] == 1
        assert kb['processed'] == 0
        assert kb['files'][0]['status'] == 'vectorization_failed'
        # File record was created (insert_new_file was called)
        mock_insert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mixed_vectorization_results_accurate_counts(self, tmp_path):
        """(c) Mixed success/failure across files: summary counts accurate."""
        sub = tmp_path / 'kb1'
        sub.mkdir()
        (sub / 'good.txt').write_text('good')
        (sub / 'bad.txt').write_text('bad')

        admin_user = {'id': 'a1', 'role': 'admin', 'email': 'a@x.com', 'name': 'A'}

        call_count = {'n': 0}

        async def alternating_process(*args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] % 2 == 0:
                raise RuntimeError('fail')

        with (
            patch.object(_plugin_module.Files, 'insert_new_file', new=AsyncMock(return_value=MagicMock())),
            patch.object(
                _plugin_module.Knowledges,
                'add_file_to_knowledge_by_id',
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(_plugin_module, 'process_file', new=alternating_process),
            patch('shutil.copy'),
            patch.object(_plugin_module, '_find_or_create_kb', new=AsyncMock(return_value=('kb-id', False))),
            patch.object(pathlib.Path, 'stat', return_value=MagicMock(st_size=10)),
            patch.object(_plugin_module, 'get_async_db'),
        ):
            async_ctx = MagicMock()
            async_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
            async_ctx.__aexit__ = AsyncMock(return_value=False)
            _plugin_module.get_async_db.return_value = async_ctx

            tools = Tools()
            tools.valves.allowed_base_dirs = [str(tmp_path)]
            result_str = await tools.import_local_directory(str(tmp_path), admin_user, MagicMock())

        data = json.loads(result_str)
        assert data['total_processed'] == 1
        assert data['total_failed'] == 1
        assert data['total_imported'] == 2
        assert data['total_linked'] == 2
