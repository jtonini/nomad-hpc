"""GitHub API client for issue submission and duplicate detection.

Supports three modes:
1. Direct API submission (with configured token)
2. Pre-filled browser URL (no token needed)
3. Email fallback (for users without GitHub accounts)
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

REPO_OWNER = "jtonini"
REPO_NAME = "nomad-hpc"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"


@dataclass
class IssueResult:
    """Result of an issue submission attempt."""

    success: bool
    url: str = ""
    number: int = 0
    method: str = ""  # "api", "browser", "email"
    error: str = ""


@dataclass
class ExistingIssue:
    """Summary of an existing issue found during duplicate search."""

    number: int
    title: str
    url: str
    state: str
    labels: list[str]
    created_at: str
    comments: int


class GitHubClient:
    """GitHub API client for issue operations."""

    # Label mappings for auto-labeling
    COMPONENT_LABELS = {
        "collectors": "component:collectors",
        "alerts": "component:alerts",
        "dashboard": "component:dashboard",
        "tessera": "component:tessera",
        "cli": "component:cli",
        "console": "component:console",
        "dynamics": "component:dynamics",
        "insights": "component:insights",
        "reference": "component:reference",
        "other": "component:other",
    }

    CATEGORY_LABELS = {
        "bug": "bug",
        "feature": "enhancement",
        "question": "question",
    }

    def __init__(self, token: str | None = None):
        self.token = token

    @property
    def has_token(self) -> bool:
        """Check if a GitHub token is configured."""
        return bool(self.token)

    def create_issue(
        self,
        title: str,
        body: str,
        category: str = "",
        component: str = "",
        version: str = "",
        institution: str = "",
        source: str = "cli",
    ) -> IssueResult:
        """Create a new issue via the GitHub API.

        Requires a configured token. Falls back to browser URL if
        token is not available.
        """
        if not self.has_token:
            url = self.generate_browser_url(title, body, category)
            return IssueResult(
                success=False,
                url=url,
                method="browser",
                error="No token configured — use browser link",
            )

        labels = self._build_labels(
            category, component, version, institution, source
        )

        payload = {
            "title": title,
            "body": body,
            "labels": labels,
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}/issues",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return IssueResult(
                    success=True,
                    url=result.get("html_url", ""),
                    number=result.get("number", 0),
                    method="api",
                )
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            return IssueResult(
                success=False,
                method="api",
                error=f"GitHub API error {e.code}: {body_text[:200]}",
            )
        except Exception as e:
            return IssueResult(
                success=False, method="api", error=str(e)
            )

    def search_duplicates(
        self, keywords: str, max_results: int = 5
    ) -> list[ExistingIssue]:
        """Search open issues for potential duplicates.

        Uses GitHub's issue search API to find similar issues.
        Works with or without a token (unauthenticated has lower
        rate limits but is sufficient for occasional searches).
        """
        # Clean up keywords for search
        words = re.sub(r"[^\w\s]", " ", keywords).split()
        if not words:
            return []
        query_terms = " ".join(words[:8])  # Limit to 8 keywords
        query = f"repo:{REPO_OWNER}/{REPO_NAME} is:issue is:open {query_terms}"

        params = urllib.parse.urlencode({
            "q": query,
            "per_page": max_results,
            "sort": "relevance",
        })

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            req = urllib.request.Request(
                f"https://api.github.com/search/issues?{params}",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                results = []
                for item in data.get("items", []):
                    results.append(
                        ExistingIssue(
                            number=item["number"],
                            title=item["title"],
                            url=item["html_url"],
                            state=item["state"],
                            labels=[
                                lb["name"] for lb in item.get("labels", [])
                            ],
                            created_at=item.get("created_at", "")[:10],
                            comments=item.get("comments", 0),
                        )
                    )
                return results
        except Exception:
            return []

    def add_comment(self, issue_number: int, body: str) -> IssueResult:
        """Add a comment to an existing issue."""
        if not self.has_token:
            url = f"{REPO_URL}/issues/{issue_number}"
            return IssueResult(
                success=False,
                url=url,
                method="browser",
                error="No token — open in browser to comment",
            )

        payload = {"body": body}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}/issues/{issue_number}/comments",
                data=data,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
                return IssueResult(
                    success=True,
                    url=result.get("html_url", ""),
                    method="api",
                )
        except Exception as e:
            return IssueResult(success=False, method="api", error=str(e))

    def generate_browser_url(
        self,
        title: str = "",
        body: str = "",
        category: str = "",
    ) -> str:
        """Generate a pre-filled GitHub new issue URL.

        Used by both the dashboard and the CLI when no token
        is configured. URL parameters pre-fill the issue form.
        """
        # Pick template based on category
        template_map = {
            "bug": "bug_report.yml",
            "feature": "feature_request.yml",
            "question": "question.yml",
        }
        template = template_map.get(category, "")

        params = {}
        if template:
            params["template"] = template
        if title:
            params["title"] = title
        if body:
            # GitHub URL body param has a length limit (~8000 chars)
            # Truncate if needed
            if len(body) > 7500:
                body = body[:7400] + "\n\n---\n*System info truncated*"
            params["body"] = body

        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        return f"{REPO_URL}/issues/new?{query}"

    def generate_email_body(
        self, title: str, body: str, category: str = ""
    ) -> tuple[str, str]:
        """Generate email subject and body for issue reporting.

        Returns (subject, body) tuple for use with mailto: or
        SMTP-based sending.
        """
        subject = f"[NØMAÐ {category.title() or 'Report'}] {title}"
        email_body = (
            f"Issue Report: {title}\n"
            f"Category: {category or 'general'}\n"
            f"{'=' * 60}\n\n"
            f"{body}\n\n"
            f"{'=' * 60}\n"
            f"Sent via nomad issue --email\n"
            f"Please create a GitHub issue at:\n"
            f"{REPO_URL}/issues/new\n"
        )
        return subject, email_body

    def _build_labels(
        self,
        category: str,
        component: str,
        version: str,
        institution: str,
        source: str,
    ) -> list[str]:
        """Build label list for auto-labeling."""
        labels = []

        if category in self.CATEGORY_LABELS:
            labels.append(self.CATEGORY_LABELS[category])

        if component in self.COMPONENT_LABELS:
            labels.append(self.COMPONENT_LABELS[component])

        if source:
            labels.append(f"source:{source}")

        if version and version != "unknown":
            labels.append(f"v{version}")

        if institution:
            # Normalize institution name for label
            inst_slug = re.sub(r"[^a-z0-9]+", "-", institution.lower()).strip("-")
            if inst_slug:
                labels.append(f"inst:{inst_slug[:30]}")

        return labels
