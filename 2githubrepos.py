from __future__ import annotations

import base64
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
            
            # Map of relative path -> absolute path
            files_to_upload: Dict[str, str] = {}

            # Recursively walk through current_directory and all subfolders
            for root, dirs, files in os.walk(current_directory):
                # Ignore hidden directories like .git or .venv
                dirs[:] = [d for d in dirs if not d.startswith(".")]

                for file_name in files:
                    # Skip hidden files if desired
                    if file_name.startswith("."):
                        continue

                    full_path = os.path.join(root, file_name)
                    
                    # Calculate the relative path from the current working directory
                    rel_path = os.path.relpath(full_path, start=current_directory)
                    
                    # Ensure path separator is '/' for GitHub API compatibility (crucial on Windows)
                    github_path = rel_path.replace(os.sep, "/")
                    
                    files_to_upload[github_path] = full_path

            if not files_to_upload:
                print("No files found in the current directory or subdirectories to upload.")
                return 0

            print(f"Found {len(files_to_upload)} file(s) across directory tree to push.\n")
            
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