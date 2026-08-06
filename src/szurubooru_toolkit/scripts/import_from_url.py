from __future__ import annotations

import glob
import json
import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from loguru import logger

from szurubooru_toolkit import config
from szurubooru_toolkit import szuru
from szurubooru_toolkit.pixiv import Pixiv
from szurubooru_toolkit.relations import RelationsBatch
from szurubooru_toolkit.scripts import upload_media
from szurubooru_toolkit.utils import convert_rating
from szurubooru_toolkit.utils import convert_tags
from szurubooru_toolkit.utils import extract_twitter_artist
from szurubooru_toolkit.utils import generate_src
from szurubooru_toolkit.utils import get_site
from szurubooru_toolkit.utils import invoke_gallery_dl
from szurubooru_toolkit.utils import run_concurrently
from szurubooru_toolkit.utils import sort_files


class TagOrigin(str, Enum):
    """Where a tag proposed by the importer came from."""

    SOURCE = 'source'
    SOURCE_MAPPED = 'source_mapped'
    TOOL_DERIVED = 'tool_derived'
    TOOL_ADDED = 'tool_added'
    USER_ADDED = 'user_added'


@dataclass
class TagCandidate:
    """A proposed final tag and all of its known origins."""

    name: str
    origins: set[TagOrigin] = field(default_factory=set)


@dataclass
class DownloadedItem:
    """A downloaded media path with normalized identifying metadata."""

    file_path: str
    raw_metadata: dict
    provider: str
    work_id: str
    source: str


@dataclass
class ArtworkBatch:
    """Downloaded pages which share tags, safety, source, and a review decision."""

    provider: str
    work_id: str
    source: str
    file_paths: list[str]
    tags: list[TagCandidate]
    safety: str
    safety_origin: str
    detected_safety: str
    detected_safety_origin: str
    selected_tags: set[str] = field(default_factory=set)
    accepted: bool = True

    def __post_init__(self) -> None:
        if not self.selected_tags:
            self.selected_tags = {tag.name for tag in self.tags}

    @property
    def final_tags(self) -> list[str]:
        """Return selected tag names in their original display order."""

        return [tag.name for tag in self.tags if tag.name in self.selected_tags]


def merge_tag_candidates(candidates: list[TagCandidate]) -> list[TagCandidate]:
    """Merge duplicate final tag names while retaining every origin."""

    merged: OrderedDict[str, TagCandidate] = OrderedDict()
    for candidate in candidates:
        name = candidate.name.strip().replace(' ', '_')
        if not name:
            continue
        if name not in merged:
            merged[name] = TagCandidate(name=name)
        merged[name].origins.update(candidate.origins)
    return list(merged.values())


def prepare_tag_candidates(metadata: dict, add_tags: list[str] | None = None) -> list[TagCandidate]:
    """Prepare final tag names and provenance without uploading anything."""

    site = metadata.get('site')
    raw_tags = metadata.get('tags') or []
    if not raw_tags and isinstance(metadata.get('tag_string'), str):
        raw_tags = metadata['tag_string'].split()
    candidates = []

    if site in ['fanbox', 'pixiv']:
        converted = convert_tags(raw_tags)
        candidates.extend(TagCandidate(tag, {TagOrigin.SOURCE_MAPPED}) for tag in converted)
    elif site == 'twitter':
        converted = convert_tags(metadata.get('hashtags') or [])
        candidates.extend(TagCandidate(tag, {TagOrigin.SOURCE_MAPPED}) for tag in converted)
    elif site in ['sankaku', 'danbooru', 'gelbooru', 'konachan', 'yandere']:
        tags = raw_tags.split() if isinstance(raw_tags, str) else raw_tags
        candidates.extend(TagCandidate(tag, {TagOrigin.SOURCE}) for tag in tags)

    artist = ''
    artist_id = None
    if site == 'e-hentai':
        for tag in raw_tags:
            if tag.startswith('artist:'):
                artist = tag.split(':', 1)[1].replace(' ', '_')
                break
    elif site in ['fanbox', 'pixiv']:
        user = metadata.get('user') or {}
        artist = user.get('name', '')
        artist_id = user.get('id')

    if artist:
        canon_artist = Pixiv.extract_pixiv_artist(artist, artist_id)
        if canon_artist:
            candidates.append(TagCandidate(canon_artist, {TagOrigin.TOOL_DERIVED}))

    if site == 'twitter':
        artist_names = extract_twitter_artist(metadata)
        if None not in artist_names:
            candidates.extend(TagCandidate(tag, {TagOrigin.TOOL_DERIVED}) for tag in artist_names)

    candidates.extend(TagCandidate(tag, {TagOrigin.TOOL_ADDED}) for tag in add_tags or [])
    return merge_tag_candidates(candidates)


def set_tags(metadata: dict) -> list[str]:
    """Compatibility wrapper returning the prepared tag names without provenance."""

    tags = [candidate.name for candidate in prepare_tag_candidates(metadata)]
    metadata['tags'] = tags
    return tags


def sort_file_by_time(file) -> datetime:
    """Return the source upload time, or the local modification time as fallback."""

    filepath = Path(file)
    filepath_json = filepath.with_suffix(filepath.suffix + '.json')
    time_value = datetime.fromtimestamp(filepath.stat().st_mtime)
    if filepath_json.exists():
        try:
            with open(filepath_json) as metadata_file:
                metadata = json.load(metadata_file)
            time_str = metadata.get('date') or metadata.get('create_date') or metadata.get('published')
            if time_str:
                time_value = datetime.fromisoformat(time_str)
        except Exception:
            pass
    return time_value


def configure_auto_tagging() -> None:
    """Mirror import-from-url tagger settings into the upload pipeline."""

    enabled = any([config.import_from_url['wd_tagger'], config.import_from_url['md5_search'], config.import_from_url['saucenao']])
    config.upload_media['auto_tag'] = enabled
    if not enabled:
        return

    config.auto_tagger['wd_tagger'] = bool(config.import_from_url['wd_tagger'])
    if not config.import_from_url['wd_tagger']:
        config.auto_tagger['wd_tagger_forced'] = False
    config.auto_tagger['md5_search'] = bool(config.import_from_url['md5_search'])
    config.auto_tagger['saucenao'] = bool(config.import_from_url['saucenao'])


def download(
    urls: list[str],
    input_file: str = '',
    verbose: bool = False,
    output_callback: Callable[[str], None] | None = None,
) -> tuple[str, list[str]]:
    """Download URLs and return the temporary directory and ordered media files."""

    if input_file and not urls:
        logger.info(f'Downloading posts from input file "{input_file}"...')
    elif input_file and urls:
        logger.info(f'Downloading posts from input file "{input_file}" and URLs {urls}...')
    else:
        logger.info(f'Downloading posts from URLs {urls}...')

    params = [f'--range={config.import_from_url["range"]}', '--write-metadata']
    if config.import_from_url['cookies']:
        params.append(f'--cookies={config.import_from_url["cookies"]}')

    gelbooru = config.credentials.get('gelbooru', {})
    user_id, api_key = gelbooru.get('user_id'), gelbooru.get('api_key')
    if user_id and api_key and 'None' not in (user_id, api_key):
        params += [
            f'--option=extractor.gelbooru.user-id={user_id}',
            f'--option=extractor.gelbooru.api-key={api_key}',
        ]
    if input_file:
        params.append(f'--input-file={input_file}')
    if not verbose:
        params.append('-q')

    download_dir = invoke_gallery_dl(
        urls,
        config.import_from_url['tmp_path'],
        params,
        workers=max(1, int(config.import_from_url['workers'])),
        output_callback=output_callback,
    )
    files = [
        file
        for file in glob.glob(f'{download_dir}/*')
        if Path(file).suffix not in ['.psd', '.json', '.zip', '.7z', '.rar', '.tar', '.gz', '.txt']
    ]
    return download_dir, sort_files(files)


def read_downloaded_item(file_path: str) -> DownloadedItem:
    """Read gallery-dl metadata and derive a provider-neutral grouping key."""

    with open(file_path + '.json') as metadata_file:
        metadata = json.load(metadata_file)

    provider = get_site(str(metadata.get('file_url', ''))) or get_site(str(metadata.get('category', ''))) or 'unknown'
    metadata['site'] = provider
    source = generate_src(metadata) or ''
    if provider == 'pixiv' and metadata.get('id') is not None:
        work_id = str(metadata['id'])
    else:
        work_id = source or file_path

    return DownloadedItem(file_path, metadata, provider, work_id, source)


def prepare_artwork_batches(
    files: list[str],
    add_tags: list[str] | None = None,
    default_safety: str = 'safe',
    safety_policy: str = 'fallback',
) -> list[ArtworkBatch]:
    """Group downloaded pages and evaluate tags and safety once per artwork."""

    grouped: OrderedDict[tuple[str, str], list[DownloadedItem]] = OrderedDict()
    for file_path in files:
        try:
            item = read_downloaded_item(file_path)
        except Exception as error:
            logger.error(f'Could not prepare metadata for {file_path}: {error}')
            continue
        grouped.setdefault((item.provider, item.work_id), []).append(item)

    batches = []
    for (provider, work_id), items in grouped.items():
        representative = items[0]
        metadata = representative.raw_metadata
        try:
            tags = prepare_tag_candidates(metadata, add_tags)
        except Exception as error:
            logger.error(f'Could not prepare tags for {provider} artwork {work_id}: {error}')
            continue

        converted_safety = convert_rating(metadata.get('rating')) if 'rating' in metadata else None
        if converted_safety in ['safe', 'sketchy', 'unsafe']:
            detected_safety = converted_safety
            detected_safety_origin = provider
        else:
            detected_safety = default_safety
            detected_safety_origin = 'tool: fallback'

        if safety_policy == 'override':
            safety = default_safety
            safety_origin = 'tool: override'
        else:
            safety = detected_safety
            safety_origin = detected_safety_origin

        batches.append(
            ArtworkBatch(
                provider=provider,
                work_id=work_id,
                source=representative.source,
                file_paths=[item.file_path for item in items],
                tags=tags,
                safety=safety,
                safety_origin=safety_origin,
                detected_safety=detected_safety,
                detected_safety_origin=detected_safety_origin,
            )
        )
    return batches


def upload_batches(
    batches: list[ArtworkBatch],
    progress_callback: Callable[[dict], None] | None = None,
    hide_progress: bool | None = None,
) -> None:
    """Upload every accepted page using already reviewed metadata."""

    if hide_progress is None:
        try:
            hide_progress = config.globals['hide_progress']
        except KeyError:
            hide_progress = config.import_from_url['hide_progress']

    relations_batch = RelationsBatch()
    jobs = [(batch, file_path) for batch in batches if batch.accepted for file_path in batch.file_paths]
    logger.info(f'Uploading {len(jobs)} post(s) from {sum(batch.accepted for batch in batches)} artwork(s)...')

    def worker(job: tuple[ArtworkBatch, str]) -> None:
        batch, file_path = job
        try:
            with open(file_path, 'rb') as media_file:
                upload_media.main(
                    file_to_upload=media_file.read(),
                    file_ext=Path(file_path).suffix[1:],
                    metadata={'tags': batch.final_tags, 'safety': batch.safety, 'source': batch.source},
                    file_path=file_path,
                    relations_batch=relations_batch,
                    progress_callback=progress_callback,
                )
        except Exception as error:
            upload_media.emit_upload_progress(progress_callback, 'failed', file_path, message=str(error))
            raise

    workers = max(1, int(config.import_from_url['workers']))
    run_concurrently(jobs, worker, workers, len(jobs), hide_progress)
    relations_batch.reconcile(szuru)


def cleanup_download(download_dir: str) -> None:
    """Remove a gallery-dl temporary directory after import or cancellation."""

    if download_dir and os.path.exists(download_dir):
        shutil.rmtree(download_dir)


@logger.catch
def main(urls: list | None = None, input_file: str = '', add_tags: list | None = None, verbose: bool = False) -> None:
    """Download, prepare, and upload URLs without interactive review."""

    configure_auto_tagging()
    download_dir = ''
    try:
        download_dir, files = download(list(urls or []), input_file, verbose)
        logger.info(f'Downloaded {len(files)} post(s). Start importing...')
        batches = prepare_artwork_batches(files, add_tags, config.upload_media['default_safety'])
        upload_batches(batches)
    finally:
        cleanup_download(download_dir)
    logger.success('Finished importing!')


if __name__ == '__main__':
    main()
