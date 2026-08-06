from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from loguru import logger
from rich.text import Text
from textual import on
from textual import work
from textual.app import App
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button
from textual.widgets import Footer
from textual.widgets import Input
from textual.widgets import ProgressBar
from textual.widgets import RichLog
from textual.widgets import Select
from textual.widgets import SelectionList
from textual.widgets import Static
from textual.widgets.selection_list import Selection

from szurubooru_toolkit import config
from szurubooru_toolkit.scripts import import_from_url
from szurubooru_toolkit.scripts.import_from_url import ArtworkBatch
from szurubooru_toolkit.scripts.import_from_url import TagCandidate
from szurubooru_toolkit.scripts.import_from_url import TagOrigin


TERMINAL_UPLOAD_EVENTS = {'uploaded', 'skipped_exact', 'skipped_similar', 'failed'}
SAFETY_LEVELS = ('safe', 'sketchy', 'unsafe')


@dataclass
class ReviewEntry:
    """One independently editable image in per-image review mode."""

    batch: ArtworkBatch
    artwork_index: int
    page_number: int
    page_total: int
    decision: str = 'pending'


class UploadProgressMessage(Message):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__()


class UploadFinishedMessage(Message):
    def __init__(self, error: str = '') -> None:
        self.error = error
        super().__init__()


class DownloadProgressMessage(Message):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        super().__init__()


class DownloadFinishedMessage(Message):
    def __init__(self, batches: list[ArtworkBatch], download_dirs: list[str], error: str = '') -> None:
        self.batches = batches
        self.download_dirs = download_dirs
        self.error = error
        super().__init__()


@dataclass
class DownloadResult:
    batches: list[ArtworkBatch]
    download_dirs: list[str]
    error: str = ''


def origin_label(tag: TagCandidate, provider: str, occurrence: str = '') -> str:
    labels = []
    if TagOrigin.SOURCE in tag.origins:
        labels.append(provider)
    if TagOrigin.SOURCE_MAPPED in tag.origins:
        labels.append(f'{provider} → canonical')
    if TagOrigin.TOOL_DERIVED in tag.origins:
        labels.append('tool-derived')
    if TagOrigin.TOOL_ADDED in tag.origins:
        labels.append('tool-added')
    if TagOrigin.USER_ADDED in tag.origins:
        labels.append('user-added')
    if occurrence:
        labels.append(occurrence)
    return ', '.join(labels)


def union_tags(batches: list[ArtworkBatch]) -> tuple[list[TagCandidate], Counter]:
    tags_by_name = {}
    occurrences = Counter()
    for batch in batches:
        for tag in batch.tags:
            occurrences[tag.name] += 1
            if tag.name not in tags_by_name:
                tags_by_name[tag.name] = TagCandidate(tag.name, set())
            tags_by_name[tag.name].origins.update(tag.origins)
    return list(tags_by_name.values()), occurrences


def source_safety_summary(batches: list[ArtworkBatch]) -> str:
    """Describe the concrete safety values selected by the fallback policy."""

    if not batches:
        return 'no artworks'

    counts = Counter(batch.detected_safety for batch in batches)
    ordered_levels = [*SAFETY_LEVELS, *sorted(set(counts).difference(SAFETY_LEVELS))]
    fallback_count = sum(batch.detected_safety_origin == 'tool: fallback' for batch in batches)

    if len(batches) == 1:
        batch = batches[0]
        origin = 'configured fallback' if batch.detected_safety_origin == 'tool: fallback' else batch.detected_safety_origin
        return f'{batch.detected_safety} ({origin})'

    summary = ', '.join(f'{level}: {counts[level]}' for level in ordered_levels if counts[level])
    if fallback_count:
        suffix = 'artwork' if fallback_count == 1 else 'artworks'
        summary += f'; fallback used for {fallback_count} {suffix}'
    return summary


def apply_shared_schema(
    batches: list[ArtworkBatch],
    tags: list[TagCandidate],
    selected: set[str],
    safety_policy: str,
    forced_safety: str,
) -> None:
    """Apply the shared overlay without copying artwork-specific source tags."""

    common_names = {
        tag.name for tag in tags if tag.name in selected and tag.origins.intersection({TagOrigin.TOOL_ADDED, TagOrigin.USER_ADDED})
    }
    tag_by_name = {tag.name: tag for tag in tags}

    for batch in batches:
        batch.selected_tags.intersection_update(selected)
        existing_names = {tag.name for tag in batch.tags}
        for name in common_names:
            if name not in existing_names:
                batch.tags.append(TagCandidate(name, set(tag_by_name[name].origins)))
            batch.selected_tags.add(name)

        if safety_policy == 'fallback':
            batch.safety = batch.detected_safety
            batch.safety_origin = batch.detected_safety_origin
        else:
            batch.safety = forced_safety
            batch.safety_origin = 'tool: shared override'


def build_review_entries(batches: list[ArtworkBatch]) -> list[ReviewEntry]:
    entries = []
    for artwork_index, batch in enumerate(batches):
        for page_number, file_path in enumerate(batch.file_paths, 1):
            page_batch = deepcopy(batch)
            page_batch.file_paths = [file_path]
            entries.append(ReviewEntry(page_batch, artwork_index, page_number, len(batch.file_paths)))
    return entries


def download_and_prepare_sources(
    jobs: list[tuple[list[str], str, str]],
    download_callback: Callable,
    prepare_callback: Callable,
    verbose: bool,
    workers: int,
    add_tags: list[str],
    default_safety: str,
    safety_policy: str,
    progress_callback: Callable[[dict], None],
) -> DownloadResult:
    """Download source jobs, report their outcomes, and prepare one review batch."""

    files: list[str] = []
    download_dirs: list[str] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=min(max(1, workers), len(jobs))) as executor:
        future_context = {}
        for urls, input_file, label in jobs:
            output: list[str] = []
            future = executor.submit(
                download_callback,
                urls,
                input_file,
                verbose,
                output_callback=output.append,
            )
            future_context[future] = (label, output)

        for future in as_completed(future_context):
            label, output = future_context[future]
            for line in output:
                progress_callback({'event': 'output', 'label': label, 'message': line})
            try:
                download_dir, downloaded_files = future.result()
                if download_dir:
                    download_dirs.append(download_dir)
                files.extend(downloaded_files)
                progress_callback(
                    {
                        'event': 'downloaded' if downloaded_files else 'empty',
                        'label': label,
                        'file_count': len(downloaded_files),
                    },
                )
            except Exception as exception:
                message = str(exception)
                partial_download_dir = getattr(exception, 'download_dir', '')
                if partial_download_dir:
                    download_dirs.append(partial_download_dir)
                errors.append(f'{label}: {message}')
                progress_callback({'event': 'failed', 'label': label, 'message': message})

    batches: list[ArtworkBatch] = []
    if files:
        progress_callback({'event': 'preparing', 'file_count': len(files)})
        try:
            batches = prepare_callback(
                files,
                add_tags=add_tags,
                default_safety=default_safety,
                safety_policy=safety_policy,
            )
            if not batches:
                errors.append('No artwork metadata could be prepared from the downloaded files.')
        except Exception as exception:
            errors.append(f'Could not prepare downloaded artwork: {exception}')
    else:
        errors.append('No supported media files were downloaded.')

    return DownloadResult(batches, download_dirs, '\n'.join(errors))


class InteractiveImportApp(App[bool]):
    """Persistent review and upload UI for interactive imports."""

    TITLE = 'Szurubooru interactive import'
    BINDINGS = [
        ('/', 'focus_filter', 'Filter tags'),
        ('a', 'focus_add', 'Add tags'),
        ('s', 'cycle_safety', 'Safety'),
        ('d', 'toggle_duplicates', 'Duplicates'),
        ('ctrl+enter', 'primary', 'Primary action'),
        ('escape', 'clear_filter', 'Clear filter'),
        ('q', 'abort', 'Abort'),
    ]

    CSS = """
    Screen {
        background: $surface;
    }

    #source-view {
        align: center middle;
        padding: 1 4;
    }

    #mode-view {
        align: center middle;
        padding: 2 6;
    }

    #source-panel, #mode-panel {
        width: 100%;
        max-width: 72;
        border: round $accent;
        padding: 1 2;
    }

    #source-panel {
        height: 100%;
        max-height: 26;
    }

    #mode-panel {
        height: auto;
    }

    #source-actions {
        height: 1;
        margin-top: 0;
    }

    #source-actions Button {
        width: 1fr;
        margin-right: 1;
    }

    #download-progress {
        margin-top: 0;
    }

    #download-summary {
        height: auto;
        min-height: 1;
        margin-top: 0;
    }

    #download-log {
        height: 1fr;
        min-height: 5;
        margin-top: 0;
        border: round $primary;
        padding: 0 1;
    }

    #mode-panel Button {
        width: 100%;
        margin-top: 1;
    }

    #review-view, #upload-view {
        padding: 0 1;
    }

    #review-header, #upload-header {
        height: auto;
        min-height: 3;
        padding: 0 1;
        border: round $accent;
    }

    #filter, #add-tags {
        margin-top: 0;
    }

    #tags {
        height: 1fr;
        min-height: 8;
        margin-top: 0;
        border: round $primary;
    }

    #controls {
        height: 1;
        margin-top: 0;
    }

    #controls Select {
        width: 1fr;
        margin-right: 1;
    }

    #similarity-threshold {
        width: 18;
    }

    #actions {
        height: 1;
        margin-top: 0;
    }

    #actions Button {
        width: 1fr;
        margin-right: 1;
    }

    #upload-summary {
        height: 2;
        padding: 0 1;
    }

    #upload-progress {
        margin: 1 1;
    }

    #upload-log {
        height: 1fr;
        min-height: 10;
        border: round $primary;
        padding: 0 1;
    }

    #upload-actions {
        height: 3;
        margin-top: 1;
    }

    #upload-actions Button {
        width: 1fr;
        margin-right: 1;
    }

    .hidden {
        display: none;
    }
    """

    def __init__(
        self,
        batches: list[ArtworkBatch],
        review_mode: str,
        safety_policy: str,
        default_safety: str,
        upload_callback: Callable = import_from_url.upload_batches,
        initial_urls: list[str] | None = None,
        input_file: str = '',
        verbose: bool = False,
        download_callback: Callable = import_from_url.download,
        prepare_callback: Callable = import_from_url.prepare_artwork_batches,
        cleanup_callback: Callable = import_from_url.cleanup_download,
    ) -> None:
        super().__init__()
        self.configured_review_mode = review_mode
        self.review_mode = review_mode
        self.initial_safety_policy = safety_policy
        self.safety_policy = safety_policy
        self.default_safety = default_safety
        self.forced_safety = default_safety
        self.upload_callback = upload_callback
        self.download_callback = download_callback
        self.prepare_callback = prepare_callback
        self.cleanup_callback = cleanup_callback
        self.initial_urls = list(initial_urls or [])
        self.input_file = input_file
        self.verbose = verbose
        self.download_active = False
        self.download_dirs: list[str] = []
        self.download_total = 0
        self.download_completed = 0

        self.batches: list[ArtworkBatch] = []
        self.shared_tags: list[TagCandidate] = []
        self.occurrences = Counter()
        self.shared_selected: set[str] = set()
        self.provider_label = 'source'
        self.entries: list[ReviewEntry] = []
        self.current_index = 0
        self._load_batches(batches)
        self.visible_tag_names: set[str] = set()
        self.loading_tags = False
        self.loading_controls = False
        self.upload_batches: list[ArtworkBatch] = []
        self.path_labels: dict[str, str] = {}
        self.upload_total = 0
        self.upload_completed = 0
        self.upload_counts = Counter()
        current_similarity = float(config.upload_media.get('max_similarity', 1.0))
        self.custom_similarity = current_similarity if current_similarity < 1 else 0.98

    def compose(self) -> ComposeResult:
        with Vertical(id='source-view', classes='hidden'):
            with Vertical(id='source-panel'):
                yield Static('[b]Import artwork[/b]\nEnter one or more URLs separated by spaces.')
                yield Input(placeholder='https://www.pixiv.net/artworks/...', id='url-input', compact=True)
                with Horizontal(id='source-actions'):
                    yield Button('Download & review', id='download', variant='primary', compact=True)
                    yield Button('Quit', id='quit-source', variant='error', compact=True)
                yield ProgressBar(total=1, id='download-progress', classes='hidden')
                yield Static('Ready.', id='download-summary')
                yield RichLog(markup=True, wrap=True, auto_scroll=True, id='download-log')

        with Vertical(id='mode-view', classes='hidden'):
            with Vertical(id='mode-panel'):
                yield Static('[b]Choose review mode[/b]\nShared schema is the fastest path for a Pixiv batch.')
                yield Button('Apply one shared tag and safety schema', id='mode-shared', variant='primary')
                yield Button('Review and queue every image', id='mode-each')

        with Vertical(id='review-view', classes='hidden'):
            yield Static(id='review-header')
            yield Input(placeholder='Filter tags (/ to focus; Esc clears)', id='filter', compact=True)
            yield SelectionList[str](id='tags')
            yield Input(placeholder='Add comma-separated tags and press Enter', id='add-tags', compact=True)
            with Horizontal(id='controls'):
                yield Select(
                    [('Keep source / fallback', 'fallback'), ('Safe', 'safe'), ('Sketchy', 'sketchy'), ('Unsafe', 'unsafe')],
                    prompt='Safety',
                    allow_blank=False,
                    value='fallback',
                    id='safety',
                    compact=True,
                )
                yield Select(
                    [('Exact matches only', 'exact'), ('Custom similarity threshold', 'custom')],
                    allow_blank=False,
                    value='exact',
                    id='duplicates',
                    compact=True,
                )
                yield Input(value=f'{self.custom_similarity:.3f}', id='similarity-threshold', classes='hidden', compact=True)
            with Horizontal(id='actions'):
                yield Button('Queue & next', id='primary', variant='primary', compact=True)
                yield Button('Skip & next', id='skip', compact=True)
                yield Button('Back', id='back', compact=True)
                yield Button('Apply to artwork', id='apply-artwork', compact=True)
                yield Button('Upload queued', id='upload-queued', variant='success', compact=True)
                yield Button('Abort', id='abort', variant='error', compact=True)

        with Vertical(id='upload-view', classes='hidden'):
            yield Static(id='upload-header')
            yield ProgressBar(total=1, id='upload-progress')
            yield Static(id='upload-summary')
            yield RichLog(markup=True, wrap=True, auto_scroll=True, id='upload-log')
            with Horizontal(id='upload-actions', classes='hidden'):
                yield Button('Import another', id='import-another', variant='success')
                yield Button('Close', id='close-upload', variant='primary')

        yield Footer()

    def on_mount(self) -> None:
        if self.batches:
            self._continue_to_review()
            return

        self._show_source()
        if self.initial_urls or self.input_file:
            self.query_one('#url-input', Input).value = ' '.join(self.initial_urls)
            self.call_after_refresh(self._start_download, self.initial_urls, self.input_file)

    def _load_batches(self, batches: list[ArtworkBatch]) -> None:
        self.batches = batches
        self.shared_tags, self.occurrences = union_tags(batches)
        self.shared_selected = {tag.name for tag in self.shared_tags}
        providers = {batch.provider for batch in batches}
        self.provider_label = next(iter(providers)) if len(providers) == 1 else 'source'
        self.entries = build_review_entries(batches)
        self.current_index = 0

    def cleanup_downloads(self) -> None:
        """Release every temporary download directory owned by this app."""

        download_dirs, self.download_dirs = self.download_dirs, []
        for download_dir in download_dirs:
            try:
                self.cleanup_callback(download_dir)
            except Exception as exception:
                logger.warning(f'Could not clean up interactive download directory "{download_dir}": {exception}')

    def _reset_for_next_import(self) -> None:
        self.cleanup_downloads()
        self._load_batches([])
        self.safety_policy = self.initial_safety_policy
        self.forced_safety = self.default_safety
        self.visible_tag_names.clear()
        self.upload_batches = []
        self.path_labels = {}
        self.upload_total = 0
        self.upload_completed = 0
        self.upload_counts.clear()
        self.download_total = 0
        self.download_completed = 0
        self.input_file = ''
        self.query_one('#url-input', Input).value = ''
        self.query_one('#download-progress').add_class('hidden')
        self.query_one('#download-summary', Static).update('Ready.')
        self.query_one('#download-log', RichLog).clear()
        self.query_one('#upload-log', RichLog).clear()
        self.query_one('#upload-actions').add_class('hidden')
        self._show_source()

    def _show_source(self) -> None:
        self.review_mode = self.configured_review_mode
        self.query_one('#mode-view').add_class('hidden')
        self.query_one('#review-view').add_class('hidden')
        self.query_one('#upload-view').add_class('hidden')
        self.query_one('#source-view').remove_class('hidden')
        self.query_one('#url-input', Input).focus()

    def _continue_to_review(self) -> None:
        self.query_one('#source-view').add_class('hidden')
        if self.configured_review_mode in {'shared', 'each'}:
            self._start_review(self.configured_review_mode)
            return
        if sum(len(batch.file_paths) for batch in self.batches) == 1:
            self._start_review('shared')
            return
        self.review_mode = 'ask'
        self.query_one('#mode-view').remove_class('hidden')
        self.query_one('#mode-shared', Button).focus()

    def _submitted_urls(self) -> list[str]:
        return self.query_one('#url-input', Input).value.split()

    def _start_download(self, urls: list[str], input_file: str = '') -> None:
        if self.download_active:
            return
        if not urls and not input_file:
            self.notify('Enter at least one URL.', severity='warning')
            self.query_one('#url-input', Input).focus()
            return

        jobs = [([url], '', url) for url in urls]
        if input_file:
            jobs.append(([], input_file, f'input file: {input_file}'))

        self.download_active = True
        self.download_total = len(jobs)
        self.download_completed = 0
        self.query_one('#url-input', Input).disabled = True
        self.query_one('#download', Button).disabled = True
        self.query_one('#quit-source', Button).disabled = True
        progress = self.query_one('#download-progress', ProgressBar)
        progress.remove_class('hidden')
        progress.update(total=self.download_total, progress=0)
        self.query_one('#download-summary', Static).update(f'Downloading 0/{self.download_total} sources…')
        log = self.query_one('#download-log', RichLog)
        log.clear()
        for _, _, label in jobs:
            log.write(Text(f'… Queued {label}', style='dim'))
        self._perform_download(jobs)

    @work(thread=True, exclusive=True, group='interactive-download')
    def _perform_download(self, jobs: list[tuple[list[str], str, str]]) -> None:
        download_settings = getattr(config, 'import_from_url', {})
        worker_count = max(1, int(download_settings.get('workers', 1)))
        result = download_and_prepare_sources(
            jobs,
            self.download_callback,
            self.prepare_callback,
            self.verbose,
            worker_count,
            config.interactive_import.get('add_tags', []),
            self.forced_safety,
            self.initial_safety_policy,
            lambda payload: self.post_message(DownloadProgressMessage(payload)),
        )
        self.post_message(DownloadFinishedMessage(result.batches, result.download_dirs, result.error))

    @on(DownloadProgressMessage)
    def _on_download_progress(self, message: DownloadProgressMessage) -> None:
        payload = message.payload
        event = payload['event']
        log = self.query_one('#download-log', RichLog)

        if event == 'preparing':
            count = payload['file_count']
            self.query_one('#download-summary', Static).update(f'Preparing {count} downloaded images for review…')
            log.write(Text(f'… Preparing metadata for {count} images', style='bold cyan'))
            return
        if event == 'output':
            if payload['message']:
                log.write(Text(f'  {payload["label"]}: {payload["message"]}', style='dim'))
            return

        self.download_completed += 1
        self.query_one('#download-progress', ProgressBar).update(progress=self.download_completed)
        self.query_one('#download-summary', Static).update(
            f'Downloaded {self.download_completed}/{self.download_total} sources',
        )
        label = payload['label']
        if event == 'downloaded':
            count = payload['file_count']
            noun = 'image' if count == 1 else 'images'
            log.write(Text(f'✓ {label} — {count} {noun}', style='bold green'))
        elif event == 'empty':
            log.write(Text(f'↷ {label} — no supported media', style='bold yellow'))
        elif event == 'failed':
            log.write(Text(f'✗ {label} — {payload["message"]}', style='bold red'))

    @on(DownloadFinishedMessage)
    def _on_download_finished(self, message: DownloadFinishedMessage) -> None:
        self.download_active = False
        self.download_dirs.extend(message.download_dirs)
        self.query_one('#url-input', Input).disabled = False
        self.query_one('#download', Button).disabled = False
        self.query_one('#quit-source', Button).disabled = False

        log = self.query_one('#download-log', RichLog)
        if message.error:
            for line in message.error.splitlines():
                log.write(Text(f'✗ {line}', style='bold red'))
        if not message.batches:
            self.query_one('#download-summary', Static).update('Download could not be prepared. Edit the URL and retry.')
            self.query_one('#url-input', Input).focus()
            return

        self._load_batches(message.batches)
        image_count = sum(len(batch.file_paths) for batch in message.batches)
        self.query_one('#download-summary', Static).update(
            f'Ready: {len(message.batches)} artworks / {image_count} images',
        )
        if message.error:
            self.notify('Some sources failed; continuing with successful downloads.', severity='warning')
        self._continue_to_review()

    def _start_review(self, mode: str) -> None:
        self.review_mode = mode
        self.query_one('#source-view').add_class('hidden')
        self.query_one('#mode-view').add_class('hidden')
        self.query_one('#review-view').remove_class('hidden')
        self._refresh_review()
        self.query_one('#tags', SelectionList).focus()

    def _current_tags_and_selection(self) -> tuple[list[TagCandidate], set[str]]:
        if self.review_mode == 'shared':
            return self.shared_tags, self.shared_selected
        entry = self.entries[self.current_index]
        return entry.batch.tags, entry.batch.selected_tags

    def _capture_visible_selection(self) -> None:
        if self.loading_tags or self.review_mode not in {'shared', 'each'}:
            return
        _, selected = self._current_tags_and_selection()
        widget = self.query_one('#tags', SelectionList)
        selected.difference_update(self.visible_tag_names)
        selected.update(widget.selected)

    def _tag_renderable(self, tag: TagCandidate) -> Text:
        occurrence = ''
        if self.review_mode == 'shared':
            occurrence = f'{self.occurrences[tag.name]}/{len(self.batches)} artworks'
        provider = self.provider_label if self.review_mode == 'shared' else self.entries[self.current_index].batch.provider
        label = origin_label(tag, provider, occurrence)
        text = Text(tag.name)
        if tag.origins.intersection({TagOrigin.TOOL_DERIVED, TagOrigin.TOOL_ADDED}):
            text.append('  ⚙', style='bold yellow')
        text.append(f'  [{label}]', style='dim cyan')
        return text

    def _populate_tags(self) -> None:
        tags, selected = self._current_tags_and_selection()
        filter_value = self.query_one('#filter', Input).value.strip().lower()
        filtered = [
            tag for tag in tags if filter_value in tag.name.lower() or filter_value in origin_label(tag, self.provider_label).lower()
        ]
        self.visible_tag_names = {tag.name for tag in filtered}
        widget = self.query_one('#tags', SelectionList)
        self.loading_tags = True
        widget.clear_options()
        widget.add_options([Selection(self._tag_renderable(tag), tag.name, tag.name in selected) for tag in filtered])
        if filtered:
            widget.highlighted = 0
        self.loading_tags = False

    def _refresh_review(self) -> None:
        self._capture_visible_selection()
        header = self.query_one('#review-header', Static)
        primary = self.query_one('#primary', Button)
        skip = self.query_one('#skip', Button)
        back = self.query_one('#back', Button)
        apply_artwork = self.query_one('#apply-artwork', Button)
        upload_queued = self.query_one('#upload-queued', Button)

        if self.review_mode == 'shared':
            image_count = sum(len(batch.file_paths) for batch in self.batches)
            safety = (
                f'source/fallback → {source_safety_summary(self.batches)}'
                if self.safety_policy == 'fallback'
                else f'forced → {self.forced_safety}'
            )
            header.update(
                f'[b]Shared schema[/b] — {len(self.batches)} artworks / {image_count} images\n'
                f'Safety: {safety} · Duplicate policy: {self._duplicate_label()} · '
                f'{len(self.shared_selected)}/{len(self.shared_tags)} tags selected',
            )
            primary.label = f'Upload {image_count} images'
            skip.add_class('hidden')
            back.add_class('hidden')
            apply_artwork.add_class('hidden')
            upload_queued.add_class('hidden')
        else:
            entry = self.entries[self.current_index]
            queued = sum(item.decision == 'queued' for item in self.entries)
            skipped = sum(item.decision == 'skipped' for item in self.entries)
            header.update(
                f'[b]Per-image review[/b] — image {self.current_index + 1}/{len(self.entries)} · '
                f'{entry.batch.provider.title()} {entry.batch.work_id} p{entry.page_number}/{entry.page_total}\n'
                f'{Path(entry.batch.file_paths[0]).name} · Queued: {queued} · Skipped: {skipped} · '
                f'Duplicate policy: {self._duplicate_label()}',
            )
            primary.label = 'Queue & next' if self.current_index < len(self.entries) - 1 else 'Queue & upload'
            skip.remove_class('hidden')
            back.remove_class('hidden')
            apply_artwork.remove_class('hidden')
            upload_queued.remove_class('hidden')
            back.disabled = self.current_index == 0
            apply_artwork.disabled = entry.page_total == 1 or entry.page_number == entry.page_total
            upload_queued.label = f'Upload queued ({queued})'
            upload_queued.disabled = queued == 0

        self._sync_controls()
        self._populate_tags()

    def _sync_controls(self) -> None:
        self.loading_controls = True
        safety = self.query_one('#safety', Select)
        if self.review_mode == 'shared':
            source_summary = source_safety_summary(self.batches)
            safety.set_options(
                [
                    (f'Use source/fallback → {source_summary}', 'fallback'),
                    ('Force safe', 'safe'),
                    ('Force sketchy', 'sketchy'),
                    ('Force unsafe', 'unsafe'),
                ],
            )
            safety.value = 'fallback' if self.safety_policy == 'fallback' else self.forced_safety
        else:
            safety.set_options([('Safe', 'safe'), ('Sketchy', 'sketchy'), ('Unsafe', 'unsafe')])
            current = self.entries[self.current_index].batch.safety
            safety.value = current if current in {'safe', 'sketchy', 'unsafe'} else 'safe'

        current_similarity = float(config.upload_media.get('max_similarity', 1.0))
        self.query_one('#duplicates', Select).value = 'exact' if current_similarity >= 1 else 'custom'
        threshold = self.query_one('#similarity-threshold', Input)
        threshold.value = f'{self.custom_similarity:.3f}'
        threshold.set_class(current_similarity >= 1, 'hidden')
        self.loading_controls = False

    def _duplicate_label(self) -> str:
        similarity = float(config.upload_media.get('max_similarity', 1.0))
        return 'exact matches only' if similarity >= 1 else f'skip above {similarity * 100:.1f}%'

    def _add_tags(self, value: str) -> None:
        tags, selected = self._current_tags_and_selection()
        for raw_name in value.split(','):
            name = raw_name.strip().replace(' ', '_')
            if not name:
                continue
            existing = next((tag for tag in tags if tag.name == name), None)
            if existing:
                existing.origins.add(TagOrigin.USER_ADDED)
            else:
                tags.append(TagCandidate(name, {TagOrigin.USER_ADDED}))
                if self.review_mode == 'shared':
                    self.occurrences[name] = 0
            selected.add(name)

    def _copy_review_state(self, source: ArtworkBatch, target: ArtworkBatch) -> None:
        target.tags = deepcopy(source.tags)
        target.selected_tags = set(source.selected_tags)
        target.safety = source.safety
        target.safety_origin = source.safety_origin

    def _advance_after_decision(self) -> None:
        if self.current_index >= len(self.entries) - 1:
            self._begin_upload(self._queued_batches())
            return
        source = self.entries[self.current_index]
        target = self.entries[self.current_index + 1]
        if source.artwork_index == target.artwork_index and target.decision == 'pending':
            self._copy_review_state(source.batch, target.batch)
        self.current_index += 1
        self.query_one('#filter', Input).value = ''
        self._refresh_review()

    def _queue_current(self) -> None:
        self._capture_visible_selection()
        self.entries[self.current_index].decision = 'queued'
        self._advance_after_decision()

    def _skip_current(self) -> None:
        self.entries[self.current_index].decision = 'skipped'
        self._advance_after_decision()

    def _apply_to_artwork(self) -> None:
        self._capture_visible_selection()
        current = self.entries[self.current_index]
        last_index = self.current_index
        for index in range(self.current_index, len(self.entries)):
            entry = self.entries[index]
            if entry.artwork_index != current.artwork_index:
                break
            self._copy_review_state(current.batch, entry.batch)
            entry.decision = 'queued'
            last_index = index
        if last_index >= len(self.entries) - 1:
            self._begin_upload(self._queued_batches())
        else:
            self.current_index = last_index + 1
            self.query_one('#filter', Input).value = ''
            self._refresh_review()

    def _queued_batches(self) -> list[ArtworkBatch]:
        return [entry.batch for entry in self.entries if entry.decision == 'queued']

    def _begin_upload(self, batches: list[ArtworkBatch]) -> None:
        if not batches:
            self.notify('No images are queued for upload.', severity='warning')
            return
        self._capture_visible_selection()
        if self.review_mode == 'shared':
            apply_shared_schema(self.batches, self.shared_tags, self.shared_selected, self.safety_policy, self.forced_safety)
            batches = [batch for batch in self.batches if batch.accepted]

        self.upload_batches = batches
        self.upload_total = sum(len(batch.file_paths) for batch in batches)
        self.path_labels = {}
        for batch in batches:
            for page_number, file_path in enumerate(batch.file_paths, 1):
                self.path_labels[file_path] = f'{batch.provider.title()} {batch.work_id} p{page_number}'

        self.query_one('#review-view').add_class('hidden')
        self.query_one('#mode-view').add_class('hidden')
        self.query_one('#upload-view').remove_class('hidden')
        self.query_one('#upload-header', Static).update(f'[b]Uploading[/b] — {self.upload_total} queued images')
        self.query_one('#upload-progress', ProgressBar).update(total=self.upload_total, progress=0)
        self._update_upload_summary()
        self._perform_upload(batches)

    @work(thread=True, exclusive=True, group='interactive-upload')
    def _perform_upload(self, batches: list[ArtworkBatch]) -> None:
        error = ''
        try:
            self.upload_callback(batches, progress_callback=self._post_upload_progress, hide_progress=True)
        except Exception as exception:
            error = str(exception)
        self.post_message(UploadFinishedMessage(error))

    def _post_upload_progress(self, payload: dict) -> None:
        self.post_message(UploadProgressMessage(payload))

    @on(UploadProgressMessage)
    def _on_upload_progress(self, message: UploadProgressMessage) -> None:
        payload = message.payload
        event = payload['event']
        file_path = payload.get('file_path')
        label = self.path_labels.get(file_path, Path(file_path).name if file_path else 'media')
        log = self.query_one('#upload-log', RichLog)

        if event == 'checking':
            log.write(Text(f'… Checking {label}', style='dim'))
        elif event == 'uploaded':
            log.write(Text(f'✓ {label} → post #{payload["post_id"]}', style='bold green'))
        elif event == 'skipped_exact':
            log.write(Text(f'↷ {label} — exact match of post #{payload["post_id"]}', style='bold yellow'))
        elif event == 'skipped_similar':
            log.write(
                Text(
                    f'↷ {label} — {payload["similarity"]:.1f}% match of post #{payload["post_id"]}',
                    style='yellow',
                ),
            )
        elif event == 'failed':
            log.write(Text(f'✗ {label} — {payload.get("message", "upload failed")}', style='bold red'))

        if event in TERMINAL_UPLOAD_EVENTS:
            self.upload_completed += 1
            self.upload_counts[event] += 1
            self.query_one('#upload-progress', ProgressBar).update(progress=self.upload_completed)
            self._update_upload_summary()

    def _update_upload_summary(self) -> None:
        self.query_one('#upload-summary', Static).update(
            f'Completed: {self.upload_completed}/{self.upload_total} · Uploaded: {self.upload_counts["uploaded"]} · '
            f'Exact duplicates: {self.upload_counts["skipped_exact"]} · '
            f'Similarity skips: {self.upload_counts["skipped_similar"]} · Failed: {self.upload_counts["failed"]}',
        )

    @on(UploadFinishedMessage)
    def _on_upload_finished(self, message: UploadFinishedMessage) -> None:
        if message.error:
            self.query_one('#upload-log', RichLog).write(Text(f'✗ Upload batch failed: {message.error}', style='bold red'))
        missing = self.upload_total - self.upload_completed
        if missing and message.error:
            self.upload_counts['failed'] += missing
            self.upload_completed += missing
            self.query_one('#upload-progress', ProgressBar).update(progress=self.upload_completed)
        self._update_upload_summary()
        self.query_one('#upload-header', Static).update('[b]Upload finished[/b] — review the activity log below')
        self.query_one('#upload-actions').remove_class('hidden')
        self.query_one('#import-another', Button).focus()

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == 'download':
            self._start_download(self._submitted_urls())
        elif button_id == 'quit-source':
            if not self.download_active:
                self.exit(False)
        elif button_id == 'mode-shared':
            self._start_review('shared')
        elif button_id == 'mode-each':
            self._start_review('each')
        elif button_id == 'primary':
            self.action_primary()
        elif button_id == 'skip':
            self._skip_current()
        elif button_id == 'back':
            if self.current_index > 0:
                self._capture_visible_selection()
                self.current_index -= 1
                self.query_one('#filter', Input).value = ''
                self._refresh_review()
        elif button_id == 'apply-artwork':
            self._apply_to_artwork()
        elif button_id == 'upload-queued':
            self._begin_upload(self._queued_batches())
        elif button_id == 'abort':
            self.action_abort()
        elif button_id == 'import-another':
            self._reset_for_next_import()
        elif button_id == 'close-upload':
            self.exit(True)

    @on(Input.Submitted, '#url-input')
    def _on_url_submitted(self) -> None:
        self._start_download(self._submitted_urls())

    @on(SelectionList.SelectedChanged)
    def _on_tags_changed(self) -> None:
        self._capture_visible_selection()
        self._refresh_header_only()

    @on(Input.Changed, '#filter')
    def _on_filter_changed(self) -> None:
        if self.review_mode in {'shared', 'each'}:
            self._capture_visible_selection()
            self._populate_tags()

    @on(Input.Submitted, '#add-tags')
    def _on_add_tags(self, event: Input.Submitted) -> None:
        self._capture_visible_selection()
        self._add_tags(event.value)
        event.input.value = ''
        self.query_one('#filter', Input).value = ''
        self._refresh_review()
        self.query_one('#tags', SelectionList).focus()

    @on(Input.Changed, '#similarity-threshold')
    def _on_similarity_changed(self, event: Input.Changed) -> None:
        if self.loading_controls:
            return
        try:
            value = float(event.value)
        except ValueError:
            return
        if 0 <= value <= 1:
            self.custom_similarity = value
            config.upload_media['max_similarity'] = value
            config.interactive_import['max_similarity'] = value
            self._refresh_header_only()

    @on(Select.Changed, '#safety')
    def _on_safety_changed(self, event: Select.Changed) -> None:
        if self.loading_controls:
            return
        value = str(event.value)
        if self.review_mode == 'shared':
            if value == 'fallback':
                self.safety_policy = 'fallback'
            else:
                self.safety_policy = 'override'
                self.forced_safety = value
        elif self.review_mode == 'each':
            batch = self.entries[self.current_index].batch
            batch.safety = value
            batch.safety_origin = 'user'
        self._refresh_header_only()

    @on(Select.Changed, '#duplicates')
    def _on_duplicate_changed(self, event: Select.Changed) -> None:
        if self.loading_controls:
            return
        threshold = self.query_one('#similarity-threshold', Input)
        if event.value == 'exact':
            config.upload_media['max_similarity'] = 1.0
            config.interactive_import['max_similarity'] = 1.0
            threshold.add_class('hidden')
        else:
            config.upload_media['max_similarity'] = self.custom_similarity
            config.interactive_import['max_similarity'] = self.custom_similarity
            threshold.remove_class('hidden')
        self._refresh_header_only()

    def _refresh_header_only(self) -> None:
        if self.review_mode not in {'shared', 'each'}:
            return
        # Header rendering also updates action counts, but does not rebuild the
        # tag list and therefore keeps keyboard focus and the highlighted row.
        header = self.query_one('#review-header', Static)
        if self.review_mode == 'shared':
            image_count = sum(len(batch.file_paths) for batch in self.batches)
            safety = (
                f'source/fallback → {source_safety_summary(self.batches)}'
                if self.safety_policy == 'fallback'
                else f'forced → {self.forced_safety}'
            )
            header.update(
                f'[b]Shared schema[/b] — {len(self.batches)} artworks / {image_count} images\n'
                f'Safety: {safety} · Duplicate policy: {self._duplicate_label()} · '
                f'{len(self.shared_selected)}/{len(self.shared_tags)} tags selected',
            )
        else:
            entry = self.entries[self.current_index]
            queued = sum(item.decision == 'queued' for item in self.entries)
            skipped = sum(item.decision == 'skipped' for item in self.entries)
            header.update(
                f'[b]Per-image review[/b] — image {self.current_index + 1}/{len(self.entries)} · '
                f'{entry.batch.provider.title()} {entry.batch.work_id} p{entry.page_number}/{entry.page_total}\n'
                f'{Path(entry.batch.file_paths[0]).name} · Queued: {queued} · Skipped: {skipped} · '
                f'Duplicate policy: {self._duplicate_label()}',
            )

    def action_focus_filter(self) -> None:
        if self.review_mode in {'shared', 'each'}:
            self.query_one('#filter', Input).focus()

    def action_focus_add(self) -> None:
        if self.review_mode in {'shared', 'each'}:
            self.query_one('#add-tags', Input).focus()

    def action_cycle_safety(self) -> None:
        if self.review_mode not in {'shared', 'each'}:
            return
        safety = self.query_one('#safety', Select)
        values = ['fallback', 'safe', 'sketchy', 'unsafe'] if self.review_mode == 'shared' else ['safe', 'sketchy', 'unsafe']
        current = str(safety.value)
        safety.value = values[(values.index(current) + 1) % len(values)] if current in values else values[0]

    def action_toggle_duplicates(self) -> None:
        if self.review_mode not in {'shared', 'each'}:
            return
        duplicate = self.query_one('#duplicates', Select)
        duplicate.value = 'custom' if duplicate.value == 'exact' else 'exact'

    def action_clear_filter(self) -> None:
        if self.review_mode not in {'shared', 'each'}:
            return
        filter_input = self.query_one('#filter', Input)
        if filter_input.value:
            filter_input.value = ''
        self.query_one('#tags', SelectionList).focus()

    def action_primary(self) -> None:
        if self.query_one('#source-view').display:
            self._start_download(self._submitted_urls())
            return
        if self.query_one('#upload-view').display:
            if self.query_one('#upload-actions').display:
                self._reset_for_next_import()
            return
        if self.review_mode == 'shared':
            self._begin_upload(self.batches)
        elif self.review_mode == 'each':
            self._queue_current()

    def action_abort(self) -> None:
        if self.download_active:
            self.notify('An active download cannot be interrupted safely.', severity='warning')
            return
        if self.query_one('#upload-view').display:
            if self.query_one('#upload-actions').display:
                self.exit(True)
            else:
                self.notify('An active upload cannot be interrupted safely.', severity='warning')
            return
        self.exit(False)


def run_interactive_import_tui(
    batches: list[ArtworkBatch],
    review_mode: str,
    safety_policy: str,
    default_safety: str,
    initial_urls: list[str] | None = None,
    input_file: str = '',
    verbose: bool = False,
) -> bool:
    app = InteractiveImportApp(
        batches,
        review_mode,
        safety_policy,
        default_safety,
        initial_urls=initial_urls,
        input_file=input_file,
        verbose=verbose,
    )
    # Ordinary stderr log sinks would paint over Textual's alternate screen.
    # Upload outcomes are delivered through structured events and rendered in
    # the activity panel instead.
    logger.disable('szurubooru_toolkit')
    try:
        return bool(app.run())
    finally:
        app.cleanup_downloads()
        logger.enable('szurubooru_toolkit')
