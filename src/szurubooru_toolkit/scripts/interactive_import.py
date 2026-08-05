from __future__ import annotations

import click
from loguru import logger

from szurubooru_toolkit import config
from szurubooru_toolkit.scripts import import_from_url
from szurubooru_toolkit.scripts.interactive_import_tui import run_interactive_import_tui


def run_once(urls: list[str], input_file: str = '', verbose: bool = False) -> bool:
    """Download and prepare one request, then run the review/upload TUI."""

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
        return run_interactive_import_tui(
            batches,
            settings['review_mode'],
            settings['safety_policy'],
            settings['default_safety'],
        )
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
