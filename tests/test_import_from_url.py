import szurubooru_toolkit


# import_from_url reads module-level globals normally created by setup_clients();
# provide stand-ins so the module can be imported in tests.
szurubooru_toolkit.szuru = None
szurubooru_toolkit.config = None

from szurubooru_toolkit.scripts import import_from_url  # noqa: E402


def test_set_tags_extracts_e_hentai_artist(monkeypatch):
    # The canonical artist lookup goes through Danbooru/szurubooru; stub it out
    monkeypatch.setattr(import_from_url.Pixiv, 'extract_pixiv_artist', staticmethod(lambda artist, artist_id=None: artist))

    metadata = {
        'site': 'e-hentai',
        'tags': ['artist:some artist', 'male:furry', 'other:full color'],
    }

    tags = import_from_url.set_tags(metadata)

    # namespaced e-hentai tags aren't imported as post tags, but the artist is
    assert tags == ['some_artist']


def test_set_tags_unknown_site_yields_no_tags():
    metadata = {'site': None, 'tags': ['artist:someone']}

    assert import_from_url.set_tags(metadata) == []


def test_extract_pixiv_artist_prefers_danbooru_url_match(monkeypatch):
    class StubDanbooru:
        def __init__(self):
            self.calls = []

        def search_artist(self, artist, by_url=False):
            self.calls.append((artist, by_url))
            return 'canonical_artist' if by_url else None

    class StubConfig:
        auto_tagger = {'use_pixiv_artist': False}

    stub = StubDanbooru()
    monkeypatch.setattr(szurubooru_toolkit, 'danbooru', stub, raising=False)
    monkeypatch.setattr(szurubooru_toolkit, 'config', StubConfig, raising=False)
    monkeypatch.setattr(szurubooru_toolkit, 'szuru', None, raising=False)

    artist = import_from_url.Pixiv.extract_pixiv_artist('Display Name', 123)

    assert artist == 'canonical_artist'
    assert stub.calls == [('users/123', True)]


def test_gelbooru_credentials_passed_to_gallery_dl(monkeypatch):
    class Cfg:
        globals = {'hide_progress': True}
        import_from_url = {
            'wd_tagger': False,
            'md5_search': False,
            'saucenao': False,
            'cookies': None,
            'range': ':1',
            'tmp_path': '/tmp/import',
            'workers': 1,
        }
        upload_media = {}
        auto_tagger = {}
        credentials = {'gelbooru': {'user_id': '123', 'api_key': 'abc'}}

    monkeypatch.setattr(import_from_url, 'config', Cfg)

    captured = {}

    def fake_invoke(urls, tmp_path, params, workers=1):
        captured['params'] = params
        raise RuntimeError('stop after building params')  # swallowed by @logger.catch

    monkeypatch.setattr(import_from_url, 'invoke_gallery_dl', fake_invoke)

    import_from_url.main(urls=['https://gelbooru.com/index.php?page=post&s=list&tags=x'])

    assert '--option=extractor.gelbooru.user-id=123' in captured['params']
    assert '--option=extractor.gelbooru.api-key=abc' in captured['params']


def test_prepare_groups_pixiv_pages_and_evaluates_tags_once(monkeypatch, tmp_path):
    class Cfg:
        auto_tagger = {'use_pixiv_artist': False}

    monkeypatch.setattr(import_from_url, 'config', Cfg)
    converted = []
    monkeypatch.setattr(import_from_url, 'convert_tags', lambda tags: converted.append(tags) or ['canonical_tag'])
    artist_lookups = []
    monkeypatch.setattr(
        import_from_url.Pixiv,
        'extract_pixiv_artist',
        staticmethod(lambda artist, artist_id=None: artist_lookups.append((artist, artist_id)) or 'canonical_artist'),
    )

    files = []
    for page in (1, 2):
        file = tmp_path / f'123_p{page}.jpg'
        file.write_bytes(b'image')
        (tmp_path / f'123_p{page}.jpg.json').write_text(
            '{"file_url":"https://www.pixiv.net/artworks/123", "id":123, "tags":["raw"],'
            ' "user":{"name":"Artist", "id":456}, "rating":"safe"}',
        )
        files.append(str(file))

    batches = import_from_url.prepare_artwork_batches(files, add_tags=['tagme'])

    assert len(batches) == 1
    assert batches[0].work_id == '123'
    assert batches[0].file_paths == files
    assert [tag.name for tag in batches[0].tags] == ['canonical_tag', 'canonical_artist', 'tagme']
    assert converted == [['raw']]
    assert import_from_url.TagOrigin.SOURCE_MAPPED in batches[0].tags[0].origins
    assert import_from_url.TagOrigin.TOOL_DERIVED in batches[0].tags[1].origins
    assert import_from_url.TagOrigin.TOOL_ADDED in batches[0].tags[2].origins
    assert artist_lookups == [('Artist', 456)]


def test_prepare_can_override_source_safety(monkeypatch, tmp_path):
    monkeypatch.setattr(import_from_url, 'convert_tags', lambda tags: [])
    monkeypatch.setattr(import_from_url.Pixiv, 'extract_pixiv_artist', staticmethod(lambda artist, artist_id=None: None))
    file = tmp_path / '123.jpg'
    file.write_bytes(b'image')
    (tmp_path / '123.jpg.json').write_text(
        '{"file_url":"https://www.pixiv.net/artworks/123", "id":123, "tags":[],'
        ' "user":{"name":"Artist"}, "rating":"explicit"}',
    )

    batch = import_from_url.prepare_artwork_batches(
        [str(file)],
        default_safety='sketchy',
        safety_policy='override',
    )[0]

    assert batch.detected_safety == 'unsafe'
    assert batch.safety == 'sketchy'
    assert batch.safety_origin == 'tool: override'


def test_prepare_unknown_source_safety_uses_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(import_from_url, 'convert_tags', lambda tags: [])
    monkeypatch.setattr(import_from_url.Pixiv, 'extract_pixiv_artist', staticmethod(lambda artist, artist_id=None: None))
    file = tmp_path / '123.jpg'
    file.write_bytes(b'image')
    (tmp_path / '123.jpg.json').write_text(
        '{"file_url":"https://www.pixiv.net/artworks/123", "id":123, "tags":[],'
        ' "user":{"name":"Artist"}, "rating":"unknown"}',
    )

    batch = import_from_url.prepare_artwork_batches([str(file)], default_safety='sketchy')[0]

    assert batch.detected_safety == 'sketchy'
    assert batch.safety == 'sketchy'
    assert batch.safety_origin == 'tool: fallback'
