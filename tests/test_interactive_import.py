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
    interactive_import = {'max_similarity': 1.0}


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
    TuiConfig.interactive_import = {'max_similarity': 1.0}
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
        app = interactive_import_tui.InteractiveImportApp([make_batch(1, [])], 'ask', 'fallback', 'safe')
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.focused.id == 'mode-shared'
            await pilot.press('enter')
            await pilot.pause()
            assert app.review_mode == 'shared'
            assert app.query_one('#review-view').display
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


def test_run_once_passes_prepared_batches_to_tui_and_cleans_up(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'each',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': ['tagme'],
            'max_similarity': 1.0,
        }

    batch = make_batch(1, [])
    captured = []
    cleaned = []
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(interactive_import.import_from_url, 'download', lambda *args: ('/tmp/download', ['1.jpg']))
    monkeypatch.setattr(interactive_import.import_from_url, 'prepare_artwork_batches', lambda *args, **kwargs: [batch])
    monkeypatch.setattr(
        interactive_import,
        'run_interactive_import_tui',
        lambda batches, mode, policy, safety: captured.append((batches, mode, policy, safety)) or True,
    )
    monkeypatch.setattr(interactive_import.import_from_url, 'cleanup_download', cleaned.append)

    assert interactive_import.run_once(['https://www.pixiv.net/artworks/1'])
    assert captured == [([batch], 'each', 'fallback', 'safe')]
    assert cleaned == ['/tmp/download']


def test_run_once_cleans_up_when_tui_aborts(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'shared',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': [],
            'max_similarity': 1.0,
        }

    cleaned = []
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(interactive_import.import_from_url, 'download', lambda *args: ('/tmp/download', ['1.jpg']))
    monkeypatch.setattr(interactive_import.import_from_url, 'prepare_artwork_batches', lambda *args, **kwargs: [make_batch(1, [])])
    monkeypatch.setattr(interactive_import, 'run_interactive_import_tui', lambda *args: False)
    monkeypatch.setattr(interactive_import.import_from_url, 'cleanup_download', cleaned.append)

    assert not interactive_import.run_once(['https://www.pixiv.net/artworks/1'])
    assert cleaned == ['/tmp/download']
