#!/usr/bin/env python3
import argparse
import base64
import requests
import sys


# ----------------------------
# Auth
# ----------------------------
def make_auth_header(username, token):
    auth = f"{username}:{token}"
    encoded = base64.b64encode(auth.encode()).decode()
    return {"Authorization": f"Token {encoded}"}


# ----------------------------
# Fetch posts (id + relations + version)
# ----------------------------
def fetch_all_posts(base_url, query, headers, limit=100):
    posts = []
    offset = 0

    while True:
        url = (
            f"{base_url}/posts/?query={query}"
            f"&offset={offset}&limit={limit}&fields=id,relations,version"
        )

        resp = requests.get(url, headers=headers)
        resp.raise_for_status()

        data = resp.json()
        results = data.get("results", [])

        if not results:
            break

        posts.extend(results)
        offset += limit

        print(f"[*] Fetched {len(posts)} posts...")

    return posts


# ----------------------------
# Update relations for one post
# ----------------------------
def update_post_relations(base_url, headers, post, all_ids, dry_run=False):
    post_id = post["id"]
    version = post["version"]

    existing_relations = [p["id"] for p in post.get("relations", [])]

    # All other posts except itself
    new_relations = [pid for pid in all_ids if pid != post_id]

    # Merge (avoid duplicates)
    merged = list(set(existing_relations + new_relations))

    if set(merged) == set(existing_relations):
        print(f"[=] Post {post_id}: already up-to-date")
        return

    print(f"[+] Post {post_id}: updating ({len(existing_relations)} → {len(merged)})")

    if dry_run:
        return

    url = f"{base_url}/post/{post_id}"
    payload = {
        "version": version,
        "relations": merged
    }

    resp = requests.put(url, json=payload, headers=headers)
    resp.raise_for_status()


# ----------------------------
# Main
# ----------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Create relations between all posts matching a query"
    )

    parser.add_argument("--url", required=True, help="Base API URL")
    parser.add_argument("--user", required=True, help="Username")
    parser.add_argument("--token", required=True, help="API token")
    parser.add_argument("--query", required=True, help='Query (e.g. "id:1000..1020")')
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    headers.update(make_auth_header(args.user, args.token))

    print("[*] Fetching posts...")
    posts = fetch_all_posts(args.url, args.query, headers, args.limit)

    if not posts:
        print("[!] No posts found")
        sys.exit(1)

    all_ids = [p["id"] for p in posts]
    print(f"[+] Total posts: {len(all_ids)}")

    print("[*] Creating relations...")
    for post in posts:
        update_post_relations(
            args.url,
            headers,
            post,
            all_ids,
            dry_run=args.dry_run
        )

    print("[+] Done!")


if __name__ == "__main__":
    main()
