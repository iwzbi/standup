#!/usr/bin/env python3
"""Bump a pinned tool: download the release asset, update version + sha256.

Usage:
  uv run python bin/bump-version.py <tool> <new-version>     # manual bump
  uv run python bin/bump-version.py --latest <tool>          # discover latest, bump one
  uv run python bin/bump-version.py --latest-all             # bump every auto:true tool

Supports two kinds of pinned tool in vars/versions.yml:
  - github_tools entries (delta, zoxide, eza, ..., gh) — has a `url` field with
    a {version} placeholder; the block is rewritten in place. An optional `alt`
    block carries a second url/sha256 pair used on dnf hosts (D018); when
    present, BOTH assets are downloaded and BOTH sha256 lines are rewritten.
  - standalone tools (neovim, lazygit) — flat <name>_version / <name>_sha256
    keys with a hardcoded URL template below.

opencode is intentionally @latest (unpinned) and is NOT bump-able here.
Comments and ordering are preserved (targeted regex edits, not a full dump).

The `auto: true` field on a github_tools entry marks it as safe to auto-bump
in the weekly CI (see .github/workflows/bump.yml). High-risk tools (neovim,
atuin, yazi, delta, lazygit) are NOT auto and need a manual `--latest <tool>`
after a human reviews the bump.

--latest-all is transactional: it discovers + downloads + hashes ALL auto
tools first, and only writes versions.yml if every download succeeded — a
mid-loop failure leaves the manifest untouched (no partial bump).

Examples:
  uv run python bin/bump-version.py zoxide 0.11.0
  uv run python bin/bump-version.py --latest zoxide
  uv run python bin/bump-version.py --latest-all
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "vars" / "versions.yml"

# Standalone tools: flat keys in versions.yml + a known URL template (linux
# x86_64 asset). Kept here (not in versions.yml) because their install roles
# don't use the github_tools list schema.
STANDALONE_TOOLS: dict[str, dict[str, str]] = {
    "neovim": {
        "version_key": "neovim_version",
        "sha256_key": "neovim_sha256",
        "url": "https://github.com/neovim/neovim/releases/download/v{version}/nvim-linux-x86_64.tar.gz",
    },
    "lazygit": {
        "version_key": "lazygit_version",
        "sha256_key": "lazygit_sha256",
        "url": "https://github.com/jesseduffield/lazygit/releases/download/v{version}/lazygit_{version}_Linux_x86_64.tar.gz",
    },
}


def load_versions() -> dict:
    import yaml  # PyYAML ships with the ansible dependency in the uv env

    return yaml.safe_load(VERSIONS_FILE.read_text())


def render_url(url_template: str, version: str) -> str:
    return url_template.replace("{version}", version)


def download_and_hash(url: str) -> str:
    with urllib.request.urlopen(url) as response:  # noqa: S310 (trusted release URLs)
        data = response.read()
    return hashlib.sha256(data).hexdigest()


def parse_github_repo(url_template: str) -> str:
    # "https://github.com/<owner>/<repo>/releases/..." -> "<owner>/<repo>"
    match = re.search(r"github\.com/([^/]+/[^/]+)/releases/", url_template)
    if not match:
        raise ValueError(f"could not parse github owner/repo from {url_template!r}")
    return match.group(1)


def get_latest_release(url_template: str) -> str:
    # GitHub's releases/latest excludes pre-releases + drafts, so this is the
    # latest STABLE release tag. Unauthenticated API (60 req/hr) is plenty.
    repo = parse_github_repo(url_template)
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        api,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "standup-bump-version",
        },
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310
        tag = json.load(response)["tag_name"]
    return tag[1:] if tag.startswith("v") else tag  # strip a single leading 'v'


def update_github_block(tool_name: str, new_version: str, new_sha256s: list[str]) -> None:
    """Rewrite `version:` and every `sha256:` line in the tool's block.

    `new_sha256s` is ordered as the lines appear: the primary asset first,
    then any `alt` assets (D018). The count must match the block's sha256
    lines exactly — one downloaded asset per pinned hash — otherwise a line
    would silently keep its stale value.
    """
    text = VERSIONS_FILE.read_text()
    # Entries are indented list items ("  - name: x"); bound the block at the
    # NEXT indented entry (or EOF) so a tool's block never spills into the
    # entries that follow it — otherwise every sha256 line from this tool to
    # the end of github_tools would be rewritten.
    pattern = re.compile(
        r"([ ]*- name:\s*" + re.escape(tool_name) + r"\b.*?)(?=\n[ ]*- name:|\Z)",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ValueError(f"github_tools entry '{tool_name}' not found")
    block = match.group(1)
    # The slice can end with the NEXT entry's leading comment block (comments
    # sit between this entry's last field and the next `- name:`). Keep those
    # out of the rewritten region so a comment that mentions `version:` or
    # `sha256:` can never be rewritten as if it were a pinned field.
    trailing = re.search(r"(?:\n[ ]*#.*)+$", block)
    tail = trailing.group(0) if trailing else ""
    core = block[: len(block) - len(tail)]

    sha_lines = re.findall(r'sha256:\s*"[^"]*"', core)
    if len(sha_lines) != len(new_sha256s):
        raise ValueError(
            f"'{tool_name}' pins {len(sha_lines)} sha256 line(s) but "
            f"{len(new_sha256s)} asset(s) were hashed"
        )
    new_core = re.sub(r'version:\s*"[^"]*"', f'version: "{new_version}"', core, count=1)

    pending = list(new_sha256s)

    def _next_sha(_match: re.Match[str]) -> str:
        return f'sha256: "{pending.pop(0)}"'

    new_core = re.sub(r'sha256:\s*"[^"]*"', _next_sha, new_core)
    if new_core == core:
        raise ValueError(f"no changes made (check that '{tool_name}' has version/sha256 lines)")
    VERSIONS_FILE.write_text(text.replace(block, new_core + tail, 1))


def update_standalone(tool_name: str, new_version: str, new_sha256: str) -> None:
    spec = STANDALONE_TOOLS[tool_name]
    text = VERSIONS_FILE.read_text()
    vkey, skey = spec["version_key"], spec["sha256_key"]
    new = re.sub(rf'{re.escape(vkey)}:\s*"[^"]*"', f'{vkey}: "{new_version}"', text)
    new = re.sub(rf'{re.escape(skey)}:\s*"[^"]*"', f'{skey}: "{new_sha256}"', new)
    if new == text:
        raise ValueError(f"no changes made (check that {vkey}/{skey} exist in {VERSIONS_FILE})")
    VERSIONS_FILE.write_text(new)


def tool_url(tool_name: str, github_tools: list[dict]) -> str:
    gh = next((t for t in github_tools if t["name"] == tool_name), None)
    if gh is not None:
        return gh["url"]
    if tool_name in STANDALONE_TOOLS:
        return STANDALONE_TOOLS[tool_name]["url"]
    raise KeyError(tool_name)


def asset_urls(gh: dict, version: str) -> list[str]:
    """Every release asset URL for `gh` at `version`: primary first, then `alt`."""
    urls = [render_url(gh["url"], version)]
    if gh.get("alt"):
        urls.append(render_url(gh["alt"]["url"], version))
    return urls


def bump_tool(tool_name: str, new_version: str, github_tools: list[dict]) -> None:
    """Download the asset(s) for new_version, compute sha256, write to versions.yml."""
    gh = next((t for t in github_tools if t["name"] == tool_name), None)
    if gh is not None:
        urls = asset_urls(gh, new_version)
        shas = []
        for url in urls:
            print(f"  downloading {url}")
            shas.append(download_and_hash(url))
        update_github_block(tool_name, new_version, shas)
        print(f"  updated: {tool_name} -> {new_version} ({len(shas)} asset(s), sha256 {shas[0][:12]}…)")
        return
    if tool_name in STANDALONE_TOOLS:
        url = render_url(STANDALONE_TOOLS[tool_name]["url"], new_version)
        print(f"  downloading {url}")
        sha = download_and_hash(url)
        update_standalone(tool_name, new_version, sha)
        print(f"  updated: {tool_name} -> {new_version} (sha256 {sha[:12]}…)")
        return
    raise KeyError(f"tool '{tool_name}' not known (available: {available_tools(github_tools)})")


def latest_all() -> int:
    """Bump every github_tools entry with `auto: true` to its latest release.

    Transactional: phase 1 discovers + downloads + hashes all auto tools
    (no writes); phase 2 writes all updates only if phase 1 fully succeeded.
    Returns the number of tools actually bumped (0 = all already current).
    """
    data = load_versions()
    github_tools = data.get("github_tools", [])
    auto_tools = [t for t in github_tools if t.get("auto")]
    if not auto_tools:
        print("no tools marked auto: true; nothing to bump")
        return 0
    # phase 1: discover + download + hash (no writes yet)
    updates: list[tuple[str, str, list[str]]] = []  # (name, new_version, [sha256, ...])
    for tool in auto_tools:
        name, current = tool["name"], tool["version"]
        latest = get_latest_release(tool["url"])
        if latest == current:
            print(f"  {name}: already at {current} (latest)")
            continue
        print(f"  {name}: {current} -> {latest}, downloading…")
        shas = []
        for url in asset_urls(tool, latest):
            shas.append(download_and_hash(url))
            print(f"    {url.rsplit('/', 1)[-1]} sha256 {shas[-1][:12]}…")
        updates.append((name, latest, shas))
    # phase 2: write all (only if phase 1 fully succeeded)
    for name, version, shas in updates:
        update_github_block(name, version, shas)
    print(f"\nbumped {len(updates)} tool(s)")
    return len(updates)


def available_tools(github_tools: list[dict]) -> str:
    return ", ".join([t["name"] for t in github_tools] + list(STANDALONE_TOOLS))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump a pinned tool: download the release asset, update version + sha256.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--latest", metavar="TOOL", help="discover latest release + bump one tool")
    parser.add_argument("--latest-all", action="store_true", help="bump every auto:true tool")
    parser.add_argument("tool", nargs="?", help="tool name (manual bump: needs <version> too)")
    parser.add_argument("version", nargs="?", help="new version (manual bump)")
    args = parser.parse_args()

    github_tools = load_versions().get("github_tools", [])

    if args.latest_all:
        latest_all()
        return
    if args.latest:
        url = tool_url(args.latest, github_tools)
        latest = get_latest_release(url)
        print(f"latest {args.latest}: {latest}")
        bump_tool(args.latest, latest, github_tools)
        return
    if args.tool and args.version:
        bump_tool(args.tool, args.version, github_tools)
        return
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
