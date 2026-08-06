from __future__ import annotations

from szurubooru_toolkit import config
from szurubooru_toolkit.scripts import import_from_url
from szurubooru_toolkit.scripts.interactive_import_tui import run_interactive_import_tui


def run_once(urls: list[str], input_file: str = '', verbose: bool = False) -> bool:
    """Start one TUI session with optional sources ready to download."""

    settings = config.interactive_import
    config.upload_media['max_similarity'] = float(settings['max_similarity'])
    return run_interactive_import_tui(
        [],
        settings['review_mode'],
        settings['safety_policy'],
        settings['default_safety'],
        initial_urls=urls,
        input_file=input_file,
        verbose=verbose,
    )


def main(urls: list[str] | None = None, input_file: str = '', verbose: bool = False) -> None:
    """Run a persistent URL/download/review/upload TUI session."""

    import_from_url.configure_auto_tagging()
    run_once(list(urls or []), input_file, verbose)
