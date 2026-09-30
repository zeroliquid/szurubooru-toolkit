import os
from urllib.parse import quote

from szurubooru_toolkit.config import Config
from szurubooru_toolkit.szurubooru import Szurubooru, SzurubooruApiError


CATEGORY = "sankaku"
CONFIRMED = os.environ.get("CONFIRM_DELETE_SANKAKU") == "yes"


def escape_search_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("-", "\\-")
    )


config = Config()
api = Szurubooru(
    config.globals["url"],
    config.globals["username"],
    config.globals["api_token"],
)

category_path = "/tag-category/" + quote(CATEGORY, safe="")
category = api._request("GET", category_path)

# Collect every tag before deleting anything, so pagination does not shift.
tags = []
offset = 0

while True:
    page = api._request(
        "GET",
        "/tags/",
        params={
            "query": f"category:{CATEGORY}",
            "limit": 100,
            "offset": offset,
        },
    )
    tags.extend(page["results"])
    offset += len(page["results"])

    if offset >= page["total"]:
        break

print(f'Category: {CATEGORY}')
print(f'Tags: {len(tags)}')
print(f'Post/tag assignments: {sum(tag["usages"] for tag in tags)}')

for tag in tags:
    print(f'  {tag["names"][0]} ({tag["usages"]} usages)')

if not CONFIRMED:
    print("\nDry run only. No changes were made.")
    print("Run again with CONFIRM_DELETE_SANKAKU=yes to proceed.")
    raise SystemExit

if category.get("default"):
    raise SystemExit(
        '"sankaku" is the default category. Set another category as default first.'
    )

target_names = {tag["names"][0] for tag in tags}

# Find all affected posts, deduplicating posts containing multiple target tags.
affected_posts = {}

for tag_name in sorted(target_names):
    query = f"tag:{escape_search_value(tag_name)}"

    for result in api.get_posts(query, videos=True):
        if not isinstance(result, str):  # The first yielded value is the count.
            affected_posts[result.id] = result

print(f"Updating {len(affected_posts)} affected posts...")

# Remove all target tags from each affected post in one update.
for index, post in enumerate(affected_posts.values(), start=1):
    for attempt in range(3):
        remaining_tags = [
            tag_name for tag_name in post.tags
            if tag_name not in target_names
        ]

        if remaining_tags == post.tags:
            break

        try:
            api._request(
                "PUT",
                f"/post/{post.id}",
                json={
                    "version": post.version,
                    "tags": remaining_tags,
                },
            )
            break
        except SzurubooruApiError as error:
            version_conflict = (
                "version" in error.description.lower()
                or "modified" in error.name.lower()
            )

            if not version_conflict or attempt == 2:
                raise

            post = api.get_post(post.id)

    print(f"\rUpdated posts: {index}/{len(affected_posts)}", end="", flush=True)

if affected_posts:
    print()

# Re-fetch each tag to obtain its latest version before deletion.
for index, tag_name in enumerate(sorted(target_names), start=1):
    tag = api.get_tag(tag_name)
    api._request(
        "DELETE",
        "/tag/" + quote(tag.primary_name, safe=""),
        json={"version": tag.version},
    )
    print(f"\rDeleted tags: {index}/{len(target_names)}", end="", flush=True)

if target_names:
    print()

# The category must now have zero usages.
category = api._request("GET", category_path)

if category["usages"] != 0:
    raise RuntimeError(
        f'Category still has {category["usages"]} tags; run the script again.'
    )

api._request(
    "DELETE",
    category_path,
    json={"version": category["version"]},
)

print('Deleted tag category "sankaku".')