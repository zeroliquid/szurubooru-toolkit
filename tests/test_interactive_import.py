import szurubooru_toolkit


szurubooru_toolkit.szuru = None
szurubooru_toolkit.config = None

from szurubooru_toolkit.scripts import interactive_import  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import ArtworkBatch  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import TagCandidate  # noqa: E402
from szurubooru_toolkit.scripts.import_from_url import TagOrigin  # noqa: E402


def make_batch(work_id, tags, safety='safe'):
    return ArtworkBatch(
        provider='pixiv',
        work_id=str(work_id),
        source=f'https://www.pixiv.net/artworks/{work_id}',
        file_paths=[f'{work_id}.jpg'],
        tags=tags,
        safety=safety,
        safety_origin='pixiv',
        detected_safety=safety,
        detected_safety_origin='pixiv',
    )


def prompt_answers(monkeypatch, answers):
    answers = iter(answers)
    monkeypatch.setattr(interactive_import.click, 'prompt', lambda *args, **kwargs: next(answers))


def test_per_artwork_review_can_remove_tool_added_tag(monkeypatch):
    batch = make_batch(
        1,
        [
            TagCandidate('source_tag', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('tagme', {TagOrigin.TOOL_ADDED}),
        ],
    )
    prompt_answers(monkeypatch, ['2', ''])

    action = interactive_import.review_artwork(batch)

    assert action == 'accept'
    assert batch.final_tags == ['source_tag']


def test_per_artwork_review_can_add_tag_and_edit_safety(monkeypatch):
    batch = make_batch(1, [])
    prompt_answers(monkeypatch, ['a', 'favorite', 's', 3, ''])

    interactive_import.review_artwork(batch)

    assert batch.final_tags == ['favorite']
    assert batch.tags[0].origins == {TagOrigin.USER_ADDED}
    assert batch.safety == 'unsafe'
    assert batch.safety_origin == 'user'


def test_safety_picker_tolerates_missing_prepared_safety(monkeypatch):
    batch = make_batch(1, [], safety=None)
    prompt_answers(monkeypatch, ['s', 2, ''])

    interactive_import.review_artwork(batch)

    assert batch.safety == 'sketchy'
    assert batch.safety_origin == 'user'


def test_review_can_adjust_similarity_policy(monkeypatch):
    class Cfg:
        upload_media = {'max_similarity': 1.0}
        interactive_import = {'max_similarity': 1.0}

    batch = make_batch(1, [])
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    prompt_answers(monkeypatch, ['m', 0.975, ''])

    interactive_import.review_artwork(batch)

    assert Cfg.upload_media['max_similarity'] == 0.975
    assert Cfg.interactive_import['max_similarity'] == 0.975


def test_shared_schema_removes_source_tag_and_adds_common_tag(monkeypatch):
    first = make_batch(
        1,
        [
            TagCandidate('keep', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('remove', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('tagme', {TagOrigin.TOOL_ADDED}),
        ],
    )
    second = make_batch(2, [TagCandidate('keep', {TagOrigin.SOURCE_MAPPED}), TagCandidate('tagme', {TagOrigin.TOOL_ADDED})])
    prompt_answers(monkeypatch, ['2', 'a', 'common', ''])

    interactive_import.review_shared_schema([first, second], 'fallback', 'safe')

    assert first.final_tags == ['keep', 'tagme', 'common']
    assert second.final_tags == ['keep', 'tagme', 'common']


def test_shared_schema_can_restore_detected_safety(monkeypatch):
    batch = make_batch(1, [], safety='unsafe')
    batch.safety = 'safe'
    batch.safety_origin = 'tool: override'
    prompt_answers(monkeypatch, ['s', 1, ''])

    interactive_import.review_shared_schema([batch], 'override', 'safe')

    assert batch.safety == 'unsafe'
    assert batch.safety_origin == 'pixiv'


def test_run_once_aborts_before_upload_and_cleans_up(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'each',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': ['tagme'],
            'max_similarity': 1.0,
        }

    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(interactive_import.import_from_url, 'download', lambda *args: ('/tmp/download', ['1.jpg']))
    monkeypatch.setattr(interactive_import.import_from_url, 'prepare_artwork_batches', lambda *args, **kwargs: [make_batch(1, [])])
    monkeypatch.setattr(interactive_import, 'review_artwork', lambda batch, *args: 'abort')
    uploaded = []
    cleaned = []
    monkeypatch.setattr(interactive_import.import_from_url, 'upload_batches', lambda batches: uploaded.append(batches))
    monkeypatch.setattr(interactive_import.import_from_url, 'cleanup_download', cleaned.append)

    assert not interactive_import.run_once(['https://www.pixiv.net/artworks/1'])
    assert uploaded == []
    assert cleaned == ['/tmp/download']


def test_each_mode_uploads_artwork_then_prompts_for_next(monkeypatch):
    class Cfg:
        upload_media = {}
        interactive_import = {
            'review_mode': 'each',
            'default_safety': 'safe',
            'safety_policy': 'fallback',
            'add_tags': ['tagme'],
            'max_similarity': 1.0,
        }

    batches = [make_batch(1, []), make_batch(2, [])]
    reviewed = []
    uploaded = []
    monkeypatch.setattr(interactive_import, 'config', Cfg)
    monkeypatch.setattr(interactive_import.import_from_url, 'download', lambda *args: ('/tmp/download', ['1.jpg', '2.jpg']))
    monkeypatch.setattr(interactive_import.import_from_url, 'prepare_artwork_batches', lambda *args, **kwargs: batches)
    monkeypatch.setattr(interactive_import, 'review_artwork', lambda batch, *args: reviewed.append(batch.work_id) or 'accept')
    monkeypatch.setattr(interactive_import.import_from_url, 'upload_batches', lambda batch: uploaded.append(batch[0].work_id))
    monkeypatch.setattr(interactive_import.import_from_url, 'cleanup_download', lambda path: None)

    assert interactive_import.run_once(['https://www.pixiv.net/users/1/artworks'])
    assert reviewed == ['1', '2']
    assert uploaded == ['1', '2']


def test_each_mode_reviews_multipage_artwork_one_image_at_a_time_and_carries_edits(monkeypatch):
    batch = make_batch(
        1,
        [
            TagCandidate('source_tag', {TagOrigin.SOURCE_MAPPED}),
            TagCandidate('tagme', {TagOrigin.TOOL_ADDED}),
        ],
    )
    batch.file_paths = ['1_p1.jpg', '1_p2.jpg']
    reviewed = []
    uploaded = []

    def review_page(state, page_number, page_total):
        reviewed.append((state.file_paths[0], page_number, page_total, list(state.final_tags), state.safety))
        if page_number == 1:
            state.selected_tags.remove('tagme')
            state.tags.append(TagCandidate('page_set', {TagOrigin.USER_ADDED}))
            state.selected_tags.add('page_set')
            state.safety = 'unsafe'
        return 'accept'

    def upload_page(batches):
        state = batches[0]
        uploaded.append((state.file_paths[0], list(state.final_tags), state.safety))

    monkeypatch.setattr(interactive_import, 'review_artwork', review_page)
    monkeypatch.setattr(interactive_import.import_from_url, 'upload_batches', upload_page)

    assert interactive_import.review_and_upload_each([batch])
    assert reviewed == [
        ('1_p1.jpg', 1, 2, ['source_tag', 'tagme'], 'safe'),
        ('1_p2.jpg', 2, 2, ['source_tag', 'page_set'], 'unsafe'),
    ]
    assert uploaded == [
        ('1_p1.jpg', ['source_tag', 'page_set'], 'unsafe'),
        ('1_p2.jpg', ['source_tag', 'page_set'], 'unsafe'),
    ]
