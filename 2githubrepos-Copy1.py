from __future__ import annotations

import base64
import fnmatch
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GITHUB_API_BASE = "https://api.github.com"
question = ";-; Enter commit reason: "
commit_context = input(question).strip()
@dataclass
class GitHubRepo:
    name: str
    full_name: str
    private: bool
    html_url: str
    clone_url: str
    ssh_url: str
    archived: bool

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "GitHubRepo":
        return cls(
            name=data.get("name", ""),
            full_name=data.get("full_name", ""),
            private=bool(data.get("private", False)),
            html_url=data.get("html_url", ""),
            clone_url=data.get("clone_url", ""),
            ssh_url=data.get("ssh_url", ""),
            archived=bool(data.get("archived", False)),
        )

class GitHubClient:
    def __init__(self, token: str, api_base: str = GITHUB_API_BASE) -> None:
        if not token:
            raise ValueError("GitHub personal access token must not be empty.")
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")

    def _build_request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[bytes] = None,
    ) -> Request:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if params:
            query = urlencode(params)
            url = f"{url}?{query}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Personal-GitHub-Repo-Grabber",
        }
        if data:
            headers["Content-Type"] = "application/json"

        return Request(url=url, headers=headers, method=method, data=data)

    def _send_request(self, request: Request) -> Any:
        try:
            with urlopen(request, timeout=15) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read().decode(charset)
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            # Read detailed error message from GitHub if available
            err_body = exc.read().decode("utf-8") if exc else ""
            raise RuntimeError(f"GitHub API Error {exc.code}: {exc.reason} - {err_body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error: {exc}") from exc

    def list_authenticated_user_repos(self) -> List[GitHubRepo]:
        repos: List[GitHubRepo] = []
        request = self._build_request("user/repos", params={"per_page": 100})
        data = self._send_request(request)
        if isinstance(data, list):
            for item in data:
                repos.append(GitHubRepo.from_api(item))
        return repos

    def upload_file_to_repo(self, owner: str, repo: str, file_path: str, file_content: bytes) -> None:
        """Uploads or updates a file directly via the GitHub Contents API."""
        path = f"repos/{owner}/{repo}/contents/{file_path}"
        
        # GitHub requires files uploaded via API to be Base64 encoded strings
        encoded_content = base64.b64encode(file_content).decode("utf-8")
        
        # First, check if the file already exists to get its 'sha' fingerprint (required for updates)
        sha = None
        try:
            check_req = self._build_request(path, method="GET")
            existing_data = self._send_request(check_req)
            if isinstance(existing_data, dict) and "sha" in existing_data:
                sha = existing_data["sha"]
        except Exception:
            # File doesn't exist yet, which is fine
            pass

        # Build the upload instructions payload
        payload = {
    "message": commit_context,  # GitHub API requires key named 'message'
    "content": encoded_content
}
        if sha:
            payload["sha"] = sha

        json_bytes = json.dumps(payload).encode("utf-8")
        upload_req = self._build_request(path, method="PUT", data=json_bytes)
        self._send_request(upload_req)


def load_token_from_env() -> str:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GitHub token not found. Please set the 'GITHUB_TOKEN' environment variable.")
    return token


# ---- .gitignore parsing & matching ----

def parse_gitignore(base_dir: str) -> List[str]:
    """Read .gitignore patterns from base_dir/.gitignore. Returns a list of patterns."""
    gitignore_path = os.path.join(base_dir, ".gitignore")
    patterns: List[str] = []
    if not os.path.isfile(gitignore_path):
        return patterns
    with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            # Skip comments and blank lines
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
    return patterns


def is_ignored(rel_path: str, patterns: List[str]) -> bool:
    """
    Check if a relative file path matches any .gitignore pattern.
    Handles directory patterns (ending with /), glob patterns, and negations (!).
    """
    ignored = False
    # Normalize the path for consistent matching
    norm_path = rel_path.replace(os.sep, "/")
    parts = norm_path.split("/")
    filename = parts[-1]

    for pattern in patterns:
        is_negation = pattern.startswith("!")
        if is_negation:
            pattern = pattern[1:]

        # Remove trailing slash — it means "match directory only"
        is_dir_pattern = pattern.endswith("/")
        match_pattern = pattern.rstrip("/")

        # Build candidate strings to test against
        candidates = [norm_path, filename]
        # Also test each parent directory segment
        for i in range(1, len(parts)):
            candidates.append("/".join(parts[i:]))

        for candidate in candidates:
            # Exact match
            if candidate == match_pattern:
                ignored = not is_negation
                continue
            # fnmatch glob (supports *, ?, [..])
            if fnmatch.fnmatch(candidate, match_pattern):
                ignored = not is_negation
                continue
            # Pattern with no slash matches any path component at any depth
            if "/" not in match_pattern:
                if fnmatch.fnmatch(filename, match_pattern):
                    ignored = not is_negation
                    continue
            # Directory pattern: match any path that starts with the dir
            if is_dir_pattern:
                for i in range(len(parts)):
                    if fnmatch.fnmatch("/".join(parts[:i + 1]), match_pattern) or fnmatch.fnmatch(parts[i], match_pattern):
                        ignored = not is_negation

    return ignored


def main() -> int:
    try:
        token = load_token_from_env()
        client = GitHubClient(token=token)

        user_response = input("Do you want to update a repo? [yes,no]: ").strip().lower()

        if user_response == "yes":
            target = input("Enter target repository as <username>/<repo-name>: ").strip()
            if "/" not in target:
                print("Error: Format must be exactly 'username/repo-name'")
                return 1
                
            owner, repo_name = target.split("/", 1)

            current_directory = os.getcwd()
            print(f"\nScanning workspace files in: {current_directory}")

            # Load .gitignore patterns
            gitignore_patterns = parse_gitignore(current_directory)
            if gitignore_patterns:
                print(f"Loaded {len(gitignore_patterns)} pattern(s) from .gitignore")
            else:
                print("⚠️  No .gitignore found — all files will be uploaded")

            # Hardcoded ignore patterns (always applied, even without .gitignore)
            always_ignore = [
                "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
                ".git", ".cache", ".config", ".local", ".npm", ".claude",
                ".ipython", ".jupyter", ".ipynb_checkpoints", "dist", "build",
                ".wrangler", ".mf", ".esbuild", "coverage", ".mypy_cache",
                ".pytest_cache", ".ruff_cache", ".eggs", "*.egg-info",
                "python_modules",
            ]

            # Map of relative path -> absolute path
            files_to_upload: Dict[str, str] = {}
            skipped_count = 0

            # Recursively walk through current_directory and all subfolders
            for root, dirs, files in os.walk(current_directory):
                # Ignore hidden directories like .git or .venv
                dirs[:] = [d for d in dirs if not d.startswith(".")]

                # Also filter out always-ignored directories
                dirs[:] = [d for d in dirs if d not in always_ignore]

                for file_name in files:
                    # Skip hidden files if desired
                    if file_name.startswith("."):
                        continue

                    full_path = os.path.join(root, file_name)
                    
                    # Calculate the relative path from the current working directory
                    rel_path = os.path.relpath(full_path, start=current_directory)
                    
                    # Ensure path separator is '/' for GitHub API compatibility (crucial on Windows)
                    github_path = rel_path.replace(os.sep, "/")

                    # Check against .gitignore patterns
                    if is_ignored(github_path, gitignore_patterns):
                        skipped_count += 1
                        continue

                    # Check against always-ignore list (by filename or directory component)
                    path_parts = github_path.split("/")
                    if any(part in always_ignore for part in path_parts):
                        skipped_count += 1
                        continue

                    files_to_upload[github_path] = full_path

            if not files_to_upload:
                print("No files found in the current directory or subdirectories to push.")
                return 0

            print(f"Found {len(files_to_upload)} file(s) to push ({skipped_count} skipped by .gitignore).\n")
            
            # Push every file sequentially maintaining folder paths
            for github_path, full_path in files_to_upload.items():
                print(f"Uploading: {github_path} ...")
                try:
                    with open(full_path, "rb") as f:
                        binary_data = f.read()
                    
                    client.upload_file_to_repo(
                        owner=owner,
                        repo=repo_name,
                        file_path=github_path,
                        file_content=binary_data
                    )
                    print(f"✅ Successfully pushed {github_path}")
                except Exception as file_err:
                    print(f"❌ Failed to upload {github_path}: {file_err}")

            print("\nAll operations finalized.")
        else:
            print("Exiting program.")
            sys.exit(0)

        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
