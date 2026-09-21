#!/usr/bin/env python3
"""Commit the generated TL;DR and open a pull request through the GitHub API.

Commits created with the ``createCommitOnBranch`` mutation are signed by GitHub,
which satisfies the repository rule that requires verified commit signatures.
"""

import base64
import os
import sys
from pathlib import Path

import httpx

API = "https://api.github.com"
GRAPHQL_QUERY = """
mutation ($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN (or GITHUB_TOKEN) is not set")

    version = os.environ["TLDR_VERSION"]
    path = os.environ["TLDR_FILE"]
    base = os.environ.get("BASE_BRANCH", "main")
    branch = f"automation/tldr-{version}"
    content = Path(path).read_text(encoding="utf-8")

    with httpx.Client(timeout=30.0, headers=headers(token)) as client:
        ref = client.get(f"{API}/repos/{repo}/git/ref/heads/{base}")
        ref.raise_for_status()
        base_head = ref.json()["object"]["sha"]

        # createCommitOnBranch requires the target branch to already exist.
        created = client.post(
            f"{API}/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_head},
        )
        if created.status_code == 422:
            existing = client.get(f"{API}/repos/{repo}/git/ref/heads/{branch}")
            existing.raise_for_status()
            expected_head = existing.json()["object"]["sha"]
        else:
            created.raise_for_status()
            expected_head = base_head

        payload = {
            "query": GRAPHQL_QUERY,
            "variables": {
                "input": {
                    "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
                    "message": {"headline": f"docs: add weekly TL;DR {version}"},
                    "fileChanges": {
                        "additions": [
                            {
                                "path": path,
                                "contents": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                            }
                        ]
                    },
                    "expectedHeadOid": expected_head,
                }
            },
        }
        response = client.post(f"{API}/graphql", json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise SystemExit(f"createCommitOnBranch failed: {data['errors']}")
        commit_url = data["data"]["createCommitOnBranch"]["commit"]["url"]

        pr = client.post(
            f"{API}/repos/{repo}/pulls",
            json={
                "title": f"Weekly TL;DR {version}",
                "head": branch,
                "base": base,
                "body": (
                    "Auto-generated from the weekly RSS digest. "
                    "Review the links and merge to publish."
                ),
            },
        )
        if pr.status_code == 422 and "already exists" in pr.text:
            print(f"pull request for {branch} already exists")
            return 0
        pr.raise_for_status()
        print(f"signed commit: {commit_url}")
        print(f"opened pull request: {pr.json()['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
