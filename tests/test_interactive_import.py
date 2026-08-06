import asyncio

import szurubooru_toolkit


szurubooru_toolkit.szuru = None
szurubooru_toolkit.config = None

from szurubooru_toolkit.scripts import interactive_import  # noqa: E402
from szurubooru_toolkit.scripts import interactive_import_tui  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import ArtworkBatch  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import TagCandidate  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import TagOrigin  # noqa: E402


class TuiConfig:
    upload_media = {'max_similarity': 1.0}
    interactive_import = {'max_similarity': 1.0, 'add_tags': []}
    import_from_url = {'workers': 1}


def make_batch(work_id, tags, safety='safe', pages=1):
    return ArtworkBatch(
        provider='pixiv',
        work_id=str(work_id),
        source=f'https://www.pixiv.net/artworks/{work_id}',
        file_paths=[f'{work_id}_p{page}.jpg' for page in range(1, pages + 1)],
        tags=tags,
        safety=safety,
        safety_origin='pixiv',
        detected_safety=safety,
        detected_safety_origin='pixiv',
    )


def wire_tui_config(monkeypatch):
    TuiConfig.upload_media = {'max_similarity': 1.0}
    TuiConfig.interactive_import = {'max_similarity': 1.0, 'add_tags': []}
    TuiConfig.import_from_url = {'workers': 1}
    monkeypatch.setattr(interactive_import_tui, 'config', TuiConfig)


def run_tui_test(coroutine):
    asyncio.run(coroutine)


def test_shared_schema_removes_source_tag_and_adds_common_tag():
    first = make_batch(
        1,
        [
            TagCandidate('keep', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('remove', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('tagme', {TagOrigin.TOOL_ADDED}),
        ],
    )
    second = make_batch(2, [TagCandidate('keep', {TagOrigin.SOURCE_MAPPED}), TagCandidate('tagme', {TagOrigin.TOOL_ADDED})])
    tags, _ = interactive_import_tui.union_tags([first, second])
    tags.append(TagCandidate('common', {TagOrigin.USER_ADDED}))

    interactive_import_tui.apply_shared_schema([first, second], tags, {'keep', 'tagme', 'common'}, 'fallback', 'safe')

    assert first.final_tags == ['keep', 'tagme', 'common']
    assert second.final_tags == ['keep', 'tagme', 'common']


def test_shared_schema_can_restore_detected_safety():
    batch = make_batch(1, [], safety='unsafe')
    batch.safety = 'safe'
    batch.safety_origin = 'tool: override'

    interactive_import_tui.apply_shared_schema([batch], [], set(), 'fallback', 'safe')

    assert batch.safety == 'unsafe'
    assert batch.safety_origin == 'pixiv'


def test_source_safety_summary_shows_value_and_origin():
    assert interactive_import_tui.source_safety_summary([make_batch(1, [], safety='sketchy')]) == 'sketchy (pixiv)'

    fallback = make_batch(2, [], safety='safe')
    fallback.detected_safety_origin = 'tool: fallback'
    assert interactive_import_tui.source_safety_summary([fallback]) == 'safe (configured fallback)'


def test_source_safety_summary_counts_mixed_batch_and_fallbacks():
    safe = make_batch(1, [], safety='safe')
    sketchy = make_batch(2, [], safety='sketchy')
    unsafe = make_batch(3, [], safety='unsafe')
    unsafe.detected_safety_origin = 'tool: fallback'

    assert (
        interactive_import_tui.source_safety_summary([safe, sketchy, unsafe])
        == 'safe: 1, sketchy: 1, unsafe: 1; fallback used for 1 artwork'
    )


def test_per_image_entries_are_independent():
    batch = make_batch(1, [TagCandidate('tagme', {TagOrigin.TOOL_ADDED})], pages=2)

    entries = interactive_import_tui.build_review_entries([batch])
    entries[0].batch.selected_tags.clear()

    assert len(entries) == 2
    assert entries[0].page_number == 1
    assert entries[1].page_number == 2
    assert entries[1].batch.final_tags == ['tagme']


def test_mode_picker_starts_with_shared_schema(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [], pages=2)], 'ask', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.focused.id == 'mode-shared'
            await pilot.press('enter')
            await pilot.pause()
            assert app.review_mode == 'shared'
            assert app.query_one('#review-view').display
            app.exit(False)

    run_tui_test(scenario())


def test_single_image_skips_mode_picker_and_uses_shared_review(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [])], 'ask', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.review_mode == 'shared'
            assert app.query_one('#review-view').display
            assert not app.query_one('#mode-view').display
            app.exit(False)

    run_tui_test(scenario())


def test_source_panel_fits_inside_short_terminal(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([], 'ask', 'fallback', 'safe')
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            source = app.query_one('#source-view')
            panel = app.query_one('#source-panel')
            assert source.content_region.contains_region(panel.region)
            app.exit(False)

    run_tui_test(scenario())


def test_shared_review_header_shows_resolved_source_safety(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [], safety='sketchy')], 'shared', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            header = app.query_one('#review-header')
            assert 'Safety: source/fallback → sketchy (pixiv)' in str(header.content)
            assert app.query_one('#tags').size.height >= 28
            app.exit(False)

    run_tui_test(scenario())


def test_space_toggles_highlighted_tag_instead_of_filtering(monkeypatch):
    wire_tui_config(monkeypatch)
    batch = make_batch(1, [TagCandidate('tagme', {TagOrigin.TOOL_ADDED})])

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([batch], 'shared', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one('#filter').value == ''
            await pilot.press('space')
            await pilot.pause()
            assert app.shared_selected == set()
            assert app.query_one('#filter').value == ''
            app.exit(False)

    run_tui_test(scenario())


def test_per_image_queue_carries_edits_to_next_page(monkeypatch):
    wire_tui_config(monkeypatch)
    batch = make_batch(1, [TagCandidate('tagme', {TagOrigin.TOOL_ADDED})], pages=2)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([batch], 'each', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press('space')
            await pilot.press('ctrl+enter')
            await pilot.pause()
            assert app.entries[0].decision == 'queued'
            assert app.current_index == 1
            assert app.entries[1].batch.final_tags == []
            app.exit(False)

    run_tui_test(scenario())


def test_safety_and_duplicate_shortcuts_update_inline_controls(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [])], 'shared', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press('s', 'd')
            await pilot.pause()
            assert app.safety_policy == 'override'
            assert app.forced_safety == 'safe'
            assert TuiConfig.upload_media['max_similarity'] == 0.98
            assert app.query_one('#similarity-threshold').display
            app.exit(False)

    run_tui_test(scenario())


def test_inline_add_tag_updates_current_review(monkeypatch):
    wire_tui_config(monkeypatch)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [])], 'shared', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one('#add-tags').focus()
            await pilot.press(*'favorite', 'enter')
            await pilot.pause()
            assert app.shared_selected == {'favorite'}
            assert app.shared_tags[0].origins == {TagOrigin.USER_ADDED}
            app.exit(False)

    run_tui_test(scenario())


def test_upload_screen_tracks_exact_matches_and_uploaded_posts(monkeypatch):
    wire_tui_config(monkeypatch)
    batch = make_batch(1, [TagCandidate('tagme', {TagOrigin.TOOL_ADDED})], pages=2)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp([batch], 'shared', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.upload_total = 2
            app.path_labels = {'1_p1.jpg': 'Pixiv 1 p1', '1_p2.jpg': 'Pixiv 1 p2'}
            app.query_one('#review-view').add_class('hidden')
            app.query_one('#upload-view').remove_class('hidden')
            app._on_upload_progress(
                interactive_import_tui.UploadProgressMessage({'event': 'uploaded', 'file_path': '1_p1.jpg', 'post_id': 10}),
            )
            app._on_upload_progress(
                interactive_import_tui.UploadProgressMessage(
                    {'event': 'skipped_exact', 'file_path': '1_p2.jpg', 'post_id': 7},
                ),
            )
            await pilot.pause()
            assert app.upload_completed == 2
            assert app.upload_counts['uploaded'] == 1
            assert app.upload_counts['skipped_exact'] == 1
            log_text = '\n'.join(line.text for line in app.query_one('#upload-log').lines)
            assert 'post #10' in log_text
            assert 'exact match of post #7' in log_text
            app.exit(True)

    run_tui_test(scenario())


def test_download_and_prepare_sources_reports_output_and_builds_batches():
    batch = make_batch(1, [])
    downloads = []
    preparations = []
    progress = []

    def download(urls, input_file, verbose, output_callback=None):
        downloads.append((urls, input_file, verbose))
        output_callback('gallery-dl test output')
        return '/tmp/download-1', ['1_p1.jpg']

    def prepare(files, **kwargs):
        preparations.append((files, kwargs))
        return [batch]

    result = interactive_import_tui.download_and_prepare_sources(
        [(['https://www.pixiv.net/artworks/1'], '', 'Pixiv 1')],
        download,
        prepare,
        False,
        1,
        [],
        'safe',
        'fallback',
        progress.append,
    )

    assert result.batches == [batch]
    assert result.download_dirs == ['/tmp/download-1']
    assert result.error == ''
    assert downloads == [(['https://www.pixiv.net/artworks/1'], '', False)]
    assert preparations[0][0] == ['1_p1.jpg']
    assert preparations[0][1]['add_tags'] == []
    assert progress == [
        {'event': 'output', 'label': 'Pixiv 1', 'message': 'gallery-dl test output'},
        {'event': 'downloaded', 'label': 'Pixiv 1', 'file_count': 1},
        {'event': 'preparing', 'file_count': 1},
    ]


def test_download_and_prepare_sources_retains_partial_directory_after_failure():
    def fail(*args, **kwargs):
        error = RuntimeError('gallery-dl failed')
        error.download_dir = '/tmp/partial-download'
        raise error

    progress = []
    result = interactive_import_tui.download_and_prepare_sources(
        [(['https://example.com/failure'], '', 'failed source')],
        fail,
        lambda *args, **kwargs: [],
        False,
        1,
        [],
        'safe',
        'fallback',
        progress.append,
    )

    assert result.batches == []
    assert result.download_dirs == ['/tmp/partial-download']
    assert 'gallery-dl failed' in result.error
    assert progress == [{'event': 'failed', 'label': 'failed source', 'message': 'gallery-dl failed'}]


def test_url_submission_opens_review_and_tracks_download(monkeypatch):
    wire_tui_config(monkeypatch)
    batch = make_batch(1, [])
    cleaned = []

    def finish_download(app, jobs):
        assert jobs == [(['https://www.pixiv.net/artworks/1'], '', 'https://www.pixiv.net/artworks/1')]
        app._on_download_progress(
            interactive_import_tui.DownloadProgressMessage(
                {'event': 'output', 'label': 'Pixiv 1', 'message': 'gallery-dl test output'},
            ),
        )
        app._on_download_progress(
            interactive_import_tui.DownloadProgressMessage(
                {'event': 'downloaded', 'label': 'Pixiv 1', 'file_count': 1},
            ),
        )
        app._on_download_finished(interactive_import_tui.DownloadFinishedMessage([batch], ['/tmp/download-1']))

    monkeypatch.setattr(interactive_import_tui.InteractiveImportApp, '_perform_download', finish_download)

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp(
            [],
            'shared',
            'fallback',
            'safe',
            cleanup_callback=cleaned.append,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one('#source-view').display
            app.query_one('#url-input').value = 'https://www.pixiv.net/artworks/1'
            app._on_url_submitted()
            assert app.query_one('#review-view').display
            assert app.batches == [batch]
            assert app.download_completed == 1
            download_log = '\n'.join(line.text for line in app.query_one('#download-log').lines)
            assert 'gallery-dl test output' in download_log
            app.cleanup_downloads()
            assert cleaned == ['/tmp/download-1']
            app.exit(False)

    run_tui_test(scenario())


def test_import_another_cleans_downloads_and_returns_to_url_entry(monkeypatch):
    wire_tui_config(monkeypatch)
    cleaned = []

    async def scenario():
        app = interactive_import_tui.InteractiveImportApp(
            [make_batch(1, [])],
            'shared',
            'fallback',
            'safe',
            cleanup_callback=cleaned.append,
        )
        app.download_dirs = ['/tmp/download-1']
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one('#review-view').add_class('hidden')
            app.query_one('#upload-view').remove_class('hidden')
            app._on_upload_finished(interactive_import_tui.UploadFinishedMessage())
            await pilot.pause()
            await pilot.click('#import-another')
            await pilot.pause()
            assert cleaned == ['/tmp/download-1']
            assert app.query_one('#source-view').display
            assert app.focused.id == 'url-input'
            assert app.batches == []
            app.exit(False)

    run_tui_test(scenario())


def test_run_once_passes_sources_to_tui(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'each',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': ['tagme'],
            'max_similarity': 1.0,
        }

    captured = []
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(
        interactive_import,
        'run_interactive_import_tui',
        lambda *args, **kwargs: captured.append((args, kwargs)) or True,
    )

    assert interactive_import.run_once(['https://www.pixiv.net/artworks/1'])
    assert captured == [
        (
            ([], 'each', 'fallback', 'safe'),
            {
                'initial_urls': ['https://www.pixiv.net/artworks/1'],
                'input_file': '',
                'verbose': False,
            },
        ),
    ]


def test_main_configures_auto_tagging_and_starts_tui(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'shared',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': [],
            'max_similarity': 1.0,
        }

    calls = []
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(interactive_import.import_from_url, 'configure_auto_tagging', lambda: calls.append('configured'))
    monkeypatch.setattr(interactive_import, 'run_once', lambda *args: calls.append(args))

    interactive_import.main()

    assert calls == ['configured', ([], '', False)]
