from time import sleep
from typing import List
from typing import Optional

import httpx
from loguru import logger


USER_AGENT = 'szurubooru-toolkit/2.0.2 (+https://github.com/reluce/szurubooru-toolkit)'


class Danbooru:
    """Handles the Danbooru API calls the toolkit needs (artists, wiki pages, tag export)."""

    def __init__(
        self,
        transport: httpx.BaseTransport = None,
        timeout: float = 5,
        retry_attempts: int = 3,
        retry_delay: float = 1,
    ) -> None:
        """
        Initialize the Danbooru client with a pooled httpx session.

        Args:
            transport (httpx.BaseTransport, optional): Custom transport, used for testing.
            timeout: Seconds to wait for one request.
            retry_attempts: Number of attempts before marking enrichment unavailable.
            retry_delay: Seconds to wait between attempts.
        """

        self.client = httpx.Client(
            base_url='https://danbooru.donmai.us',
            headers={'User-Agent': USER_AGENT},
            timeout=timeout,
            transport=transport,
        )
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delay = max(0, retry_delay)
        # Tag and artist enrichment is optional. Once the service is known to
        # be unreachable, do not spend the retry budget again for every tag in
        # the same import.
        self.available = True
        self.last_error = ''

    def _mark_unavailable(self, error: Exception) -> None:
        self.available = False
        self.last_error = f'{type(error).__name__}: {error}'
        logger.warning('Danbooru is unavailable; skipping remaining tag and artist lookups for this run.')

    def get_other_names_tag(self, other_tag: str) -> Optional[str]:
        """
        Search for the main tag name of the given tag.

        This method searches for the main tag name of the supplied tag on Danbooru via its wiki pages. A connection
        failure retries within the configured short budget, then disables optional enrichment for the rest of the run.
        If the tag is not found, it returns None.

        Args:
            other_tag (str): The tag you want to search for.

        Returns:
            Optional[str]: The main tag if found, None otherwise.
        """

        if not self.available:
            return None

        failure = None
        for _ in range(self.retry_attempts):
            try:
                params = {'search[other_names_match]': other_tag, 'only': 'title'}
                tag = self.client.get('/wiki_pages.json', params=params).json()[0]['title']

                logger.debug(f'Returning found tag for {other_tag}: {tag}')

                break
            except (IndexError, KeyError, ValueError):
                logger.debug(f'Could not find tag for other_tag "{other_tag}"')
                tag = None

                break
            except (TimeoutError, httpx.HTTPError) as error:
                failure = error
                if _ < self.retry_attempts - 1:
                    logger.debug(f'Could not establish connection to Danbooru, trying again in {self.retry_delay}s...')
                    sleep(self.retry_delay)
        else:
            logger.debug('Could not establish connection to Danbooru. Skip search for other tag...')
            self._mark_unavailable(failure)
            tag = None

        return tag

    @staticmethod
    def _artist_score(artist: dict) -> int:
        """Rank ambiguous Danbooru artist matches using the historical heuristic."""

        other_names = artist.get('other_names') or []
        group_bonus = 2 if artist.get('group_name') else 0
        try:
            created_year = int(str(artist.get('created_at', '9999'))[:4])
        except ValueError:
            created_year = 9999
        return len(other_names) * 3 + group_bonus - created_year

    @classmethod
    def _choose_artist(cls, artists: list[dict]) -> Optional[str]:
        """Choose one artist response, applying the heuristic when ambiguous."""

        if not artists:
            return None
        if len(artists) > 1:
            logger.debug(f'Found {len(artists)} artists. Choosing by score.')
            return max(artists, key=cls._artist_score)['name']
        return artists[0]['name']

    def search_artist(self, artist: str, by_url: bool = False) -> Optional[str]:
        """
        Search for the main artist name on Danbooru and return it.

        This method searches for the main artist name on Danbooru by URL, or first by base name and then by other names.
        Ambiguous URL/alias results are ranked by alias count, group membership, and record age. A connection failure
        retries within the configured short budget, then disables optional enrichment for the rest of the run. If the
        artist is not found, it returns None.

        Args:
            artist (str): The artist name. Can be an alias as well.
            by_url (bool, optional): Match a Danbooru artist URL instead of a name. Defaults to False.

        Returns:
            Optional[str]: The main artist name if found, None otherwise.
        """

        if not self.available:
            return None

        search_term = artist
        failure = None
        for _ in range(self.retry_attempts):
            try:
                if by_url:
                    params = {'search[url_matches]': artist.lower(), 'search[is_deleted]': 'false'}
                else:
                    params = {'search[name]': artist.lower()}

                response = self.client.get('/artists.json', params=params)
                response.raise_for_status()
                result = response.json()

                if result and not by_url:
                    artist = result[0]['name']
                else:
                    if not by_url:
                        params = {'search[any_other_name_like]': artist.lower(), 'search[is_deleted]': 'false'}
                        response = self.client.get('/artists.json', params=params)
                        response.raise_for_status()
                        result = response.json()
                    matched_artist = self._choose_artist(result)
                    if not matched_artist:
                        raise IndexError
                    artist = matched_artist

                logger.debug(f'Returning artist: {artist}')

                break
            except (IndexError, KeyError):
                logger.debug(f'Could not find artist "{search_term.lower()}"')
                artist = None

                break
            except ValueError:
                logger.debug(f'Could not load JSON for artist {artist}')
                artist = None

                break
            except (TimeoutError, httpx.HTTPError) as error:
                failure = error
                if _ < self.retry_attempts - 1:
                    logger.debug(f'Could not establish connection to Danbooru, trying again in {self.retry_delay}s...')
                    sleep(self.retry_delay)
        else:
            logger.debug('Could not establish connection to Danbooru. Skip this artist...')
            self._mark_unavailable(failure)
            artist = None

        return artist

    def download_tags(self, query: str = '*', min_post_count: int = 10, limit: int = 100) -> List[dict]:
        """
        Download and return tags from Danbooru.

        This method downloads tags from Danbooru. It builds the request with the provided query, minimum post count,
        and limit, and yields the tags page by page.

        Args:
            query (str, optional): Search for specific tag, accepts wildcard (*). If not specified, download all tags.
                                Defaults to '*'.
            min_post_count (int, optional): The minimum amount of posts the tag should have been used in. Defaults to 10.
            limit (int, optional): The amount of tags that should be downloaded. Start from the most recent ones.
                                Defaults to 100.

        Yields:
            List[dict]: A page of found tags.
        """

        if limit > 1000:
            pages = limit // 1000
        else:
            pages = 1

        for page in range(1, pages + 1):
            params = {
                'search[post_count]': f'>{min_post_count}',
                'search[name_matches]': query,
                'limit': limit,
                'page': page,
            }

            try:
                logger.info(f'Fetching tags from Danbooru, page {page}...')
                tags = self.client.get('/tags.json', params=params).json()
                if not isinstance(tags, list):
                    logger.warning(f'Unexpected Danbooru response on page {page}: {tags!r}')
                    continue
                yield tags
            except Exception as e:
                logger.critical(f'Could not fetch tags: {e}')

    # ponytail: 100 names per request keeps URLs well below length limits
    CHUNK_SIZE = 100

    def get_tag_implications(self, tag_names: List[str]) -> dict:
        """
        Returns the active Danbooru tag implications for the given tags.

        Args:
            tag_names (List[str]): The antecedent tag names to look up.

        Returns:
            dict: A mapping of antecedent tag name to a list of implied tag names.
        """

        implications = {}

        for index in range(0, len(tag_names), self.CHUNK_SIZE):
            chunk = tag_names[index : index + self.CHUNK_SIZE]
            params = {
                'search[antecedent_name_comma]': ','.join(chunk),
                'search[status]': 'active',
                'limit': 1000,
            }

            try:
                results = self.client.get('/tag_implications.json', params=params).json()
                if not isinstance(results, list):
                    logger.warning(f'Unexpected Danbooru response: {results!r}')
                    continue
                for entry in results:
                    implications.setdefault(entry['antecedent_name'], []).append(entry['consequent_name'])
            except Exception as e:
                logger.critical(f'Could not fetch tag implications: {e}')

        return implications

    def get_tag_categories(self, tag_names: List[str]) -> dict:
        """
        Returns the Danbooru categories of the given tags.

        Args:
            tag_names (List[str]): The tag names to look up.

        Returns:
            dict: A mapping of tag name to its numerical Danbooru category.
        """

        categories = {}

        for index in range(0, len(tag_names), self.CHUNK_SIZE):
            chunk = tag_names[index : index + self.CHUNK_SIZE]
            params = {'search[name_comma]': ','.join(chunk), 'limit': 1000}

            try:
                results = self.client.get('/tags.json', params=params).json()
                if not isinstance(results, list):
                    logger.warning(f'Unexpected Danbooru response: {results!r}')
                    continue
                for entry in results:
                    categories[entry['name']] = entry['category']
            except Exception as e:
                logger.critical(f'Could not fetch tag categories: {e}')

        return categories
