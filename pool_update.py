#!/usr/bin/env python3
import argparse
import base64
import requests
import sys


# ----------------------------
# Auth helper
# ----------------------------
def make_auth_header(username, token):
    auth = f"{username}:{token}"
    encoded = base64.b64encode(auth.encode()).decode()
    return {"Authorization": f"Token {encoded}"}


# ----------------------------
# Fetch posts (with pagination)
# ----------------------------
def fetch_all_posts(base_url, query, headers, limit=100):
    posts = []
    offset = 0

    while True:
        url = f"{base_url}/posts/?query={query}&offset={offset}&limit={limit}"
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()

        data = resp.json()
        results = data.get("results", [])

        if not results:
            break

        posts.extend(results)
        offset += limit

        print(f"[*] Fetched {len(posts)} posts so far...")

    return posts


# ----------------------------
# Get pool
# ----------------------------
def get_pool(base_url, headers, pool_id):
    url = f"{base_url}/pool/{pool_id}"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ----------------------------
# Update pool (merge or replace)
# ----------------------------
def update_pool(base_url, headers, pool_id, new_post_ids, replace=False):
    pool = get_pool(base_url, headers, pool_id)

    version = pool["version"]
    existing_posts = [p["id"] for p in pool.get("posts", [])]

    if replace:
        final_posts = list(set(new_post_ids))
        print("[*] Replacing pool contents")
    else:
        final_posts = list(set(existing_posts + new_post_ids))
        print("[*] Merging with existing pool")

    print(f"[*] Existing posts: {len(existing_posts)}")
    print(f"[*] New posts: {len(new_post_ids)}")
    print(f"[*] Final total: {len(final_posts)}")

    url = f"{base_url}/pool/{pool_id}"
    payload = {
        "version": version,
        "posts": final_posts
    }

    resp = requests.put(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ----------------------------
# Main CLI
# ----------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Query Szurubooru and update an existing pool"
    )

    parser.add_argument("--url", required=True, help="Base API URL (e.g. https://booru/api)")
    parser.add_argument("--user", required=True, help="Username")
    parser.add_argument("--token", required=True, help="API token")
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--pool-id", required=True, type=int, help="Pool ID to update")
    parser.add_argument("--limit", type=int, default=100, help="Page size")
    parser.add_argument("--replace", action="store_true", help="Replace pool instead of merging")

    args = parser.parse_args()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    headers.update(make_auth_header(args.user, args.token))

    # Step 1: fetch posts
    print("[*] Fetching posts...")
    posts = fetch_all_posts(args.url, args.query, headers, args.limit)

    if not posts:
        print("[!] No posts found.")
        sys.exit(1)

    post_ids = [p["id"] for p in posts]
    print(f"[+] Total posts fetched: {len(post_ids)}")

    # Step 2: update pool
    print("[*] Updating pool...")
    pool = update_pool(
        args.url,
        headers,
        args.pool_id,
        post_ids,
        replace=args.replace
    )

    print("[+] Pool updated successfully!")
    print(f"Pool ID: {pool.get('id')}")


if __name__ == "__main__":
    main()
