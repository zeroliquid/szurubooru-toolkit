from __future__ import annotations

from collections import Counter
from copy import deepcopy

import click
from loguru import logger

from szurubooru_toolkit import config
from szurubooru_toolkit.scripts import import_from_url
from szurubooru_toolkit.scripts.import_from_url import ArtworkBatch
from szurubooru_toolkit.scripts.import_from_url import TagCandidate
from szurubooru_toolkit.scripts.import_from_url import TagOrigin


class ReviewAborted(Exception):
    """Raised when the user cancels before upload."""


def _origin_label(tag: TagCandidate, provider: str, occurrence: str = '') -> str:
    labels = []
    if TagOrigin.SOURCE in tag.origins:
        labels.append(provider)
    if TagOrigin.SOURCE_MAPPED in tag.origins:
        labels.append(f'{provider} -> canonical')
    if TagOrigin.TOOL_DERIVED in tag.origins:
        labels.append('tool-derived')
    if TagOrigin.TOOL_ADDED in tag.origins:
        labels.append('tool-added')
    if TagOrigin.USER_ADDED in tag.origins:
        labels.append('user-added')
    if occurrence:
        labels.append(occurrence)
    return ', '.join(labels)


def _echo_tag(index: int, tag: TagCandidate, selected: set[str], provider: str, occurrence: str = '') -> None:
    mark = 'x' if tag.name in selected else ' '
    label = _origin_label(tag, provider, occurrence)
    if TagOrigin.TOOL_ADDED in tag.origins:
        styled_label = click.style(f'[{label}]', fg='yellow', bold=True)
    elif TagOrigin.USER_ADDED in tag.origins:
        styled_label = click.style(f'[{label}]', fg='green')
    elif TagOrigin.TOOL_DERIVED in tag.origins:
        styled_label = click.style(f'[{label}]', fg='cyan')
    else:
        styled_label = click.style(f'[{label}]', fg='blue')
    click.echo(f'{index:>3}. [{mark}] {tag.name:<28} {styled_label}')


def _toggle_indices(value: str, tags: list[TagCandidate], selected: set[str]) -> bool:
    """Toggle one or more comma/space-separated checkbox indexes."""

    try:
        indexes = [int(part) for part in value.replace(',', ' ').split()]
    except ValueError:
        return False
    if not indexes or any(index < 1 or index > len(tags) for index in indexes):
        return False
    for index in indexes:
        name = tags[index - 1].name
        if name in selected:
            selected.remove(name)
        else:
            selected.add(name)
    return True


def _add_user_tags(tags: list[TagCandidate], selected: set[str]) -> None:
    value = click.prompt('Tags to add (comma-separated)', default='', show_default=False)
    for raw_name in value.split(','):
        name = raw_name.strip().replace(' ', '_')
        if not name:
            continue
        existing = next((tag for tag in tags if tag.name == name), None)
        if existing:
            existing.origins.add(TagOrigin.USER_ADDED)
        else:
            tags.append(TagCandidate(name, {TagOrigin.USER_ADDED}))
        selected.add(name)


def _choose_safety(current: str) -> str:
    choices = ['safe', 'sketchy', 'unsafe']
    current = current if current in choices else 'safe'
    click.echo('\nSafety')
    for index, choice in enumerate(choices, 1):
        mark = 'x' if choice == current else ' '
        click.echo(f'  {index}. ({mark}) {choice}')
    index = click.prompt('Choose safety', type=click.IntRange(1, 3), default=choices.index(current) + 1)
    return choices[index - 1]


def _current_max_similarity() -> float:
    try:
        return float(config.upload_media['max_similarity'])
    except (AttributeError, KeyError, TypeError, ValueError):
        return 1.0


def _similarity_policy_label() -> str:
    max_similarity = _current_max_similarity()
    if max_similarity >= 1:
        return 'exact matches only'
    return f'skip above {max_similarity * 100:.1f}% similarity'


def _edit_max_similarity() -> None:
    click.echo('\nSimilarity policy')
    click.echo('Use 1.0 to upload every non-exact image, even when it is visually similar.')
    value = click.prompt(
        'Maximum allowed similarity',
        type=click.FloatRange(0, 1),
        default=_current_max_similarity(),
    )
    config.upload_media['max_similarity'] = value
    config.interactive_import['max_similarity'] = value


def review_artwork(batch: ArtworkBatch, page_number: int | None = None, page_total: int | None = None) -> str:
    """Edit one image in place and return accept, skip, or abort."""

    while True:
        heading = f'\n{batch.provider.title()} artwork {batch.work_id}'
        if page_number is not None and page_total is not None:
            heading += f' - image {page_number} of {page_total}'
        else:
            heading += f' - {len(batch.file_paths)} page(s)'
        click.echo(heading)
        click.echo(f'Source: {batch.source or "unknown"}')
        click.echo(f'Safety: {batch.safety} [{batch.safety_origin}]')
        click.echo(f'Similarity: {_similarity_policy_label()}')
        click.echo('\nTags')
        if batch.tags:
            for index, tag in enumerate(batch.tags, 1):
                _echo_tag(index, tag, batch.selected_tags, batch.provider)
        else:
            click.echo('  (none)')
        click.echo('\nEnter accepts; numbers toggle; a adds tags; s edits safety; m edits similarity; x skips; q aborts.')
        command = click.prompt('Action', default='', show_default=False).strip().lower()
        if not command:
            return 'accept'
        if command == 'a':
            _add_user_tags(batch.tags, batch.selected_tags)
        elif command == 's':
            batch.safety = _choose_safety(batch.safety)
            batch.safety_origin = 'user'
        elif command == 'm':
            _edit_max_similarity()
        elif command == 'x':
            batch.accepted = False
            return 'skip'
        elif command == 'q':
            return 'abort'
        elif not _toggle_indices(command, batch.tags, batch.selected_tags):
            click.echo('Unknown action. Enter tag numbers such as "2 4", or use a, s, m, x, or q.', err=True)


def _union_tags(batches: list[ArtworkBatch]) -> tuple[list[TagCandidate], Counter]:
    tags_by_name = {}
    occurrences = Counter()
    for batch in batches:
        for tag in batch.tags:
            occurrences[tag.name] += 1
            if tag.name not in tags_by_name:
                tags_by_name[tag.name] = TagCandidate(tag.name, set())
            tags_by_name[tag.name].origins.update(tag.origins)
    return list(tags_by_name.values()), occurrences


def review_shared_schema(batches: list[ArtworkBatch], safety_policy: str, default_safety: str) -> None:
    """Review a tag overlay and safety policy once, then apply it to all artworks."""

    tags, occurrences = _union_tags(batches)
    selected = {tag.name for tag in tags}
    policy = safety_policy
    forced_safety = default_safety
    providers = {batch.provider for batch in batches}
    provider_label = next(iter(providers)) if len(providers) == 1 else 'source'

    while True:
        click.echo(f'\nShared schema - {len(batches)} artwork(s), {sum(len(batch.file_paths) for batch in batches)} page(s)')
        if policy == 'fallback':
            click.echo('Safety: retain source safety, with configured fallback')
        else:
            click.echo(f'Safety: force {forced_safety}')
        click.echo(f'Similarity: {_similarity_policy_label()}')
        click.echo('\nTags')
        for index, tag in enumerate(tags, 1):
            occurrence = f'{occurrences[tag.name]}/{len(batches)} artworks'
            _echo_tag(index, tag, selected, provider_label, occurrence)
        if not tags:
            click.echo('  (none)')
        click.echo('\nSelected source tags remain only where detected; selected tool/user tags apply to every artwork.')
        click.echo('Enter accepts; numbers toggle; a adds common tags; s edits safety; m edits similarity; q aborts.')
        command = click.prompt('Action', default='', show_default=False).strip().lower()
        if not command:
            break
        if command == 'a':
            before = {tag.name for tag in tags}
            _add_user_tags(tags, selected)
            for tag in tags:
                if tag.name not in before:
                    occurrences[tag.name] = 0
        elif command == 's':
            click.echo('\n  1. Retain source safety with fallback')
            click.echo('  2. Force safe')
            click.echo('  3. Force sketchy')
            click.echo('  4. Force unsafe')
            choice = click.prompt('Safety policy', type=click.IntRange(1, 4), default=1 if policy == 'fallback' else 2)
            if choice == 1:
                policy = 'fallback'
            else:
                policy = 'override'
                forced_safety = ['safe', 'sketchy', 'unsafe'][choice - 2]
        elif command == 'm':
            _edit_max_similarity()
        elif command == 'q':
            raise ReviewAborted
        elif not _toggle_indices(command, tags, selected):
            click.echo('Unknown action. Enter tag numbers such as "2 4", or use a, s, m, or q.', err=True)

    common_names = {
        tag.name
        for tag in tags
        if tag.name in selected and tag.origins.intersection({TagOrigin.TOOL_ADDED, TagOrigin.USER_ADDED})
    }
    selected_names = {tag.name for tag in tags if tag.name in selected}
    tag_by_name = {tag.name: tag for tag in tags}

    for batch in batches:
        batch.selected_tags.intersection_update(selected_names)
        existing_names = {tag.name for tag in batch.tags}
        for name in common_names:
            if name not in existing_names:
                candidate = tag_by_name[name]
                batch.tags.append(TagCandidate(name, set(candidate.origins)))
            batch.selected_tags.add(name)

        if policy == 'fallback':
            batch.safety = batch.detected_safety
            batch.safety_origin = batch.detected_safety_origin
        else:
            batch.safety = forced_safety
            batch.safety_origin = 'tool: shared override'


def choose_review_mode(default: str = 'each') -> str:
    """Prompt for per-image or shared review."""

    click.echo('\nReview mode')
    click.echo('  1. Review every image')
    click.echo('  2. Apply one shared schema')
    choice = click.prompt('Choose mode', type=click.IntRange(1, 2), default=1 if default == 'each' else 2)
    return 'each' if choice == 1 else 'shared'


def review_and_upload_each(batches: list[ArtworkBatch]) -> bool:
    """Review and immediately upload one image before moving to the next."""

    total_images = sum(len(batch.file_paths) for batch in batches)
    current_image = 0
    processed_images = 0

    for batch in batches:
        file_paths = list(batch.file_paths)
        # Reuse the reviewed state within a multipage artwork. Page 2 therefore
        # starts with page 1's tag and safety edits, but remains independently editable.
        review_state = deepcopy(batch)
        for page_number, file_path in enumerate(file_paths, 1):
            current_image += 1
            review_state.file_paths = [file_path]
            review_state.accepted = True
            click.echo(f'\nImage {current_image} of {total_images}')
            action = review_artwork(review_state, page_number, len(file_paths))
            if action == 'abort':
                if processed_images:
                    click.echo(
                        f'Import stopped after processing {processed_images} image(s). Earlier uploads were kept.',
                    )
                else:
                    click.echo('Import cancelled; nothing was uploaded.')
                return processed_images > 0
            if action == 'skip':
                continue

            import_from_url.upload_batches([review_state])
            processed_images += 1
            if current_image < total_images:
                click.echo(f'Finished image {current_image} of {total_images}; continuing to the next image.')
            else:
                click.echo(f'Finished image {current_image} of {total_images}.')

    if processed_images:
        logger.success(f'Finished importing {processed_images} image(s)!')
        return True

    click.echo('Nothing selected for upload.')
    return False


def show_summary(batches: list[ArtworkBatch]) -> None:
    accepted = [batch for batch in batches if batch.accepted]
    skipped = len(batches) - len(accepted)
    click.echo('\nImport summary')
    click.echo(f'  Artworks: {len(accepted)} accepted, {skipped} skipped')
    click.echo(f'  Pages:    {sum(len(batch.file_paths) for batch in accepted)}')
    for batch in accepted:
        click.echo(
            f'  - {batch.provider} {batch.work_id}: {len(batch.file_paths)} page(s), '
            f'{len(batch.final_tags)} tag(s), safety {batch.safety}',
        )


def run_once(urls: list[str], input_file: str = '', verbose: bool = False) -> bool:
    """Download one request, review it fully, and upload after confirmation."""

    settings = config.interactive_import
    config.upload_media['max_similarity'] = float(settings['max_similarity'])
    download_dir = ''
    try:
        download_dir, files = import_from_url.download(urls, input_file, verbose)
        logger.info(f'Downloaded {len(files)} post(s). Preparing review...')
        if not files:
            click.echo('No media files were downloaded.')
            return False
        batches = import_from_url.prepare_artwork_batches(
            files,
            add_tags=settings['add_tags'],
            default_safety=settings['default_safety'],
            safety_policy=settings['safety_policy'],
        )
        mode = choose_review_mode() if settings['review_mode'] == 'ask' else settings['review_mode']
        if mode == 'each':
            return review_and_upload_each(batches)

        review_shared_schema(batches, settings['safety_policy'], settings['default_safety'])
        show_summary(batches)
        if not any(batch.accepted for batch in batches):
            click.echo('Nothing selected for upload.')
            return False
        if not click.confirm('Upload this import?', default=True):
            click.echo('Import cancelled; nothing was uploaded.')
            return False
        import_from_url.upload_batches(batches)
        logger.success('Finished importing!')
        return True
    except ReviewAborted:
        click.echo('Import cancelled; nothing was uploaded.')
        return False
    finally:
        import_from_url.cleanup_download(download_dir)


def main(urls: list[str] | None = None, input_file: str = '', verbose: bool = False) -> None:
    """Run supplied URLs once, or recreate the old helper's repeating URL prompt."""

    import_from_url.configure_auto_tagging()
    if urls or input_file:
        run_once(list(urls or []), input_file, verbose)
        return

    while True:
        value = click.prompt('\nURL (empty to quit)', default='', show_default=False).strip()
        if not value:
            return
        run_once([value], verbose=verbose)
