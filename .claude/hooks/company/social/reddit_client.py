#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.28.0"]
# ///
"""
Reddit Client — OAuth2-authenticated Reddit API integration.

This module provides Reddit posting integration for Forge, implementing:
- OAuth2 authentication (script application type)
- Text and link post submission
- Comment/reply functionality
- Rate limit tracking and backoff
- Comprehensive error handling

Authentication uses environment variables:
- FORGE_REDDIT_CLIENT_ID
- FORGE_REDDIT_CLIENT_SECRET
- FORGE_REDDIT_USERNAME
- FORGE_REDDIT_PASSWORD

Supports project-specific prefixes (e.g., MYPROJECT_REDDIT_CLIENT_ID).

Usage:
    from social.reddit_client import RedditClient, PostContent

    client = RedditClient()
    client.authenticate()

    # Submit a text post
    result = client.post(PostContent(
        text="This is the post body",
        subreddit="testsubreddit",
        title="My Post Title"
    ))

    # Submit a link post
    result = client.post_to_subreddit(
        subreddit="testsubreddit",
        title="Check this out",
        link="https://example.com"
    )

    # Reply to a post/comment
    result = client.reply(post_id="t3_abc123", content="Great post!")
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

# Configure module logger
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------


@dataclass
class PostContent:
    """Content for a Reddit post or comment."""

    text: str
    media: Optional[list[str]] = (
        None  # Reddit doesn't support direct media upload via API
    )
    reply_to: Optional[str] = None
    subreddit: Optional[str] = None
    title: Optional[str] = None
    link: Optional[str] = None


@dataclass
class PostResult:
    """Result of a Reddit post operation."""

    success: bool
    post_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RateLimitStatus:
    """Current rate limit status."""

    requests_remaining: int
    requests_used: int
    reset_timestamp: float
    window_seconds: int = 60


@dataclass
class RedditCredentials:
    """Reddit API credentials."""

    client_id: str
    client_secret: str
    username: str
    password: str


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Reddit API endpoints
REDDIT_AUTH_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_API_BASE = "https://oauth.reddit.com"

# Reddit API rate limits (60 requests per minute)
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # Exponential backoff base
RETRY_JITTER = 0.1  # Random jitter factor

# User agent (required by Reddit API TOS)
USER_AGENT = "python:forge-social:v1.0 (by /u/forge-automation)"

# Environment variable prefixes to try
ENV_PREFIXES = ["FORGE", ""]


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class RedditAuthError(Exception):
    """Raised when Reddit authentication fails."""

    pass


class RedditAPIError(Exception):
    """Raised when Reddit API returns an error."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response: Optional[dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class RedditRateLimitError(RedditAPIError):
    """Raised when rate limit is exceeded."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class RedditSubredditError(RedditAPIError):
    """Raised for subreddit-specific errors (banned, restricted, etc.)."""

    pass


# -----------------------------------------------------------------------------
# Rate Limiter
# -----------------------------------------------------------------------------


class RateLimiter:
    """
    Token bucket rate limiter for Reddit API.

    Reddit allows 60 requests per minute. This class tracks usage
    and enforces limits with automatic reset.
    """

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_REQUESTS,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests_made = 0
        self.window_start = time.time()

    def _reset_if_needed(self) -> None:
        """Reset counter if window has elapsed."""
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.requests_made = 0
            self.window_start = now

    def can_proceed(self) -> bool:
        """Check if we can make another request."""
        self._reset_if_needed()
        return self.requests_made < self.max_requests

    def record_request(self) -> None:
        """Record that a request was made."""
        self._reset_if_needed()
        self.requests_made += 1

    def wait_time(self) -> float:
        """Return seconds to wait before next request is allowed."""
        self._reset_if_needed()
        if self.can_proceed():
            return 0.0
        return self.window_seconds - (time.time() - self.window_start)

    def get_status(self) -> RateLimitStatus:
        """Get current rate limit status."""
        self._reset_if_needed()
        return RateLimitStatus(
            requests_remaining=self.max_requests - self.requests_made,
            requests_used=self.requests_made,
            reset_timestamp=self.window_start + self.window_seconds,
            window_seconds=self.window_seconds,
        )


# -----------------------------------------------------------------------------
# Reddit Client
# -----------------------------------------------------------------------------


class RedditClient:
    """
    Reddit API client with OAuth2 authentication.

    Implements the SocialClient protocol for Forge social integration.
    Uses Reddit's "script" application type for server-to-server auth.

    Attributes:
        credentials: Reddit API credentials (loaded from environment).
        access_token: Current OAuth2 access token.
        token_expires: Timestamp when token expires.
        rate_limiter: Rate limit tracker.
    """

    def __init__(
        self,
        credentials: Optional[RedditCredentials] = None,
        project_prefix: Optional[str] = None,
    ):
        """
        Initialize Reddit client.

        Args:
            credentials: Optional credentials (loads from env if not provided).
            project_prefix: Optional project prefix for env vars (e.g., "MYPROJECT").
        """
        self.credentials = credentials
        self.project_prefix = project_prefix
        self.access_token: Optional[str] = None
        self.token_expires: float = 0.0
        self.rate_limiter = RateLimiter()
        self._session: Optional[Any] = None

    def _get_env_var(self, name: str) -> Optional[str]:
        """
        Get environment variable with prefix fallback.

        Tries prefixes in order:
        1. Project-specific prefix (if set)
        2. FORGE_ prefix
        3. No prefix

        Args:
            name: Variable name without prefix (e.g., "REDDIT_CLIENT_ID").

        Returns:
            Value if found, None otherwise.
        """
        prefixes = []
        if self.project_prefix:
            prefixes.append(self.project_prefix)
        prefixes.extend(ENV_PREFIXES)

        for prefix in prefixes:
            var_name = f"{prefix}_{name}" if prefix else name
            value = os.environ.get(var_name)
            if value:
                return value
        return None

    def _load_credentials(self) -> RedditCredentials:
        """
        Load credentials from environment variables.

        Returns:
            RedditCredentials with loaded values.

        Raises:
            RedditAuthError: If required credentials are missing.
        """
        client_id = self._get_env_var("REDDIT_CLIENT_ID")
        client_secret = self._get_env_var("REDDIT_CLIENT_SECRET")
        username = self._get_env_var("REDDIT_USERNAME")
        password = self._get_env_var("REDDIT_PASSWORD")

        missing = []
        if not client_id:
            missing.append("REDDIT_CLIENT_ID")
        if not client_secret:
            missing.append("REDDIT_CLIENT_SECRET")
        if not username:
            missing.append("REDDIT_USERNAME")
        if not password:
            missing.append("REDDIT_PASSWORD")

        if missing:
            raise RedditAuthError(
                f"Missing required environment variables: {', '.join(missing)}. "
                f"Set with FORGE_ prefix or project prefix: {self.project_prefix}"
            )

        return RedditCredentials(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
        )

    def _get_session(self) -> Any:
        """Get or create requests session."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": USER_AGENT})
        return self._session

    def _is_token_valid(self) -> bool:
        """Check if current access token is still valid."""
        if not self.access_token:
            return False
        # Add 60 second buffer before expiry
        return time.time() < (self.token_expires - 60)

    def authenticate(self) -> bool:
        """
        Authenticate with Reddit OAuth2.

        Uses "script" application type with password grant.
        Token is cached and refreshed automatically.

        Returns:
            True if authentication successful.

        Raises:
            RedditAuthError: If authentication fails.
        """
        if self._is_token_valid():
            logger.debug("Using cached access token")
            return True

        if not self.credentials:
            self.credentials = self._load_credentials()

        logger.info("Authenticating with Reddit API")

        session = self._get_session()

        auth_data = {
            "grant_type": "password",
            "username": self.credentials.username,
            "password": self.credentials.password,
        }

        try:
            response = session.post(
                REDDIT_AUTH_URL,
                auth=(self.credentials.client_id, self.credentials.client_secret),
                data=auth_data,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )

            if response.status_code == 401:
                raise RedditAuthError("Invalid client credentials")

            if response.status_code == 400:
                error_data = response.json() if response.text else {}
                error_msg = error_data.get("error", "Bad request")
                if error_msg == "invalid_grant":
                    raise RedditAuthError("Invalid username or password")
                raise RedditAuthError(f"Authentication failed: {error_msg}")

            response.raise_for_status()

            data = response.json()
            self.access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self.token_expires = time.time() + expires_in

            if not self.access_token:
                raise RedditAuthError("No access token in response")

            logger.info("Successfully authenticated with Reddit")
            return True

        except Exception as e:
            if isinstance(e, RedditAuthError):
                raise
            logger.error(f"Authentication error: {e}")
            raise RedditAuthError(f"Authentication failed: {e}")

    def _api_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
        retry: bool = True,
    ) -> dict:
        """
        Make authenticated API request with rate limiting and retry.

        Args:
            method: HTTP method (GET, POST, etc.).
            endpoint: API endpoint path.
            data: POST data.
            params: Query parameters.
            retry: Whether to retry on transient errors.

        Returns:
            Response JSON data.

        Raises:
            RedditAPIError: On API errors.
            RedditRateLimitError: On rate limit exceeded.
        """
        # Ensure authenticated
        if not self._is_token_valid():
            self.authenticate()

        # Check rate limit
        if not self.rate_limiter.can_proceed():
            wait_time = self.rate_limiter.wait_time()
            logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
            time.sleep(wait_time)

        url = urljoin(REDDIT_API_BASE, endpoint)
        session = self._get_session()
        headers = {
            "Authorization": f"bearer {self.access_token}",
            "User-Agent": USER_AGENT,
        }

        last_error: Optional[Exception] = None
        retries = MAX_RETRIES if retry else 1

        for attempt in range(retries):
            try:
                logger.debug(
                    f"API request: {method} {endpoint} (attempt {attempt + 1})"
                )

                response = session.request(
                    method=method,
                    url=url,
                    data=data,
                    params=params,
                    headers=headers,
                    timeout=30,
                )

                self.rate_limiter.record_request()

                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limited (429), retry after {retry_after}s")
                    if attempt < retries - 1:
                        time.sleep(retry_after)
                        continue
                    raise RedditRateLimitError(
                        "Rate limit exceeded",
                        retry_after=retry_after,
                    )

                # Handle auth errors
                if response.status_code == 401:
                    logger.warning("Token expired, re-authenticating")
                    self.access_token = None
                    self.authenticate()
                    if attempt < retries - 1:
                        continue
                    raise RedditAuthError("Authentication failed after retry")

                # Handle other errors
                if response.status_code >= 400:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("message", response.text[:200])
                    raise RedditAPIError(
                        f"API error: {error_msg}",
                        status_code=response.status_code,
                        response=error_data,
                    )

                return response.json()

            except RedditAPIError:
                raise
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    backoff = RETRY_BACKOFF_BASE**attempt
                    logger.warning(f"Request failed: {e}, retrying in {backoff}s")
                    time.sleep(backoff)
                    continue
                raise RedditAPIError(f"Request failed after {retries} attempts: {e}")

        raise RedditAPIError(f"Request failed: {last_error}")

    def post(self, content: PostContent) -> PostResult:
        """
        Create a new post on Reddit.

        Args:
            content: Post content including text, subreddit, and optional title/link.

        Returns:
            PostResult with success status and post details.
        """
        if not content.subreddit:
            return PostResult(success=False, error="Subreddit is required")

        if content.reply_to:
            return self.reply(content.reply_to, content.text)

        if content.link:
            return self.post_to_subreddit(
                subreddit=content.subreddit,
                title=content.title or content.text[:100],
                link=content.link,
            )

        return self.post_to_subreddit(
            subreddit=content.subreddit,
            title=content.title or "",
            content=content.text,
        )

    def post_to_subreddit(
        self,
        subreddit: str,
        title: str,
        content: Optional[str] = None,
        link: Optional[str] = None,
    ) -> PostResult:
        """
        Post to a specific subreddit.

        Args:
            subreddit: Subreddit name (without r/ prefix).
            title: Post title.
            content: Text content (for self posts).
            link: URL (for link posts).

        Returns:
            PostResult with success status.
        """
        # Clean subreddit name
        subreddit = subreddit.lstrip("r/").strip()

        if not title:
            return PostResult(success=False, error="Title is required")

        if not content and not link:
            return PostResult(success=False, error="Either content or link is required")

        logger.info(f"Posting to r/{subreddit}: {title[:50]}...")

        try:
            # Determine post type
            if link:
                data = {
                    "sr": subreddit,
                    "kind": "link",
                    "title": title,
                    "url": link,
                    "api_type": "json",
                }
            else:
                data = {
                    "sr": subreddit,
                    "kind": "self",
                    "title": title,
                    "text": content,
                    "api_type": "json",
                }

            response = self._api_request("POST", "/api/submit", data=data)

            # Parse response
            json_data = response.get("json", {})
            errors = json_data.get("errors", [])

            if errors:
                error_msg = self._parse_submit_errors(errors, subreddit)
                logger.error(f"Post submission error: {error_msg}")
                return PostResult(success=False, error=error_msg)

            data_section = json_data.get("data", {})
            post_id = data_section.get("id") or data_section.get("name")
            post_url = data_section.get("url")

            logger.info(f"Successfully posted to r/{subreddit}: {post_id}")

            return PostResult(
                success=True,
                post_id=post_id,
                url=post_url,
            )

        except RedditSubredditError as e:
            return PostResult(success=False, error=str(e))
        except RedditAPIError as e:
            logger.error(f"API error posting to r/{subreddit}: {e}")
            return PostResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Unexpected error posting to r/{subreddit}: {e}")
            return PostResult(success=False, error=f"Unexpected error: {e}")

    def _parse_submit_errors(self, errors: list, subreddit: str) -> str:
        """Parse Reddit submit API errors into human-readable message."""
        error_messages = []

        for error in errors:
            if isinstance(error, list) and len(error) >= 2:
                error_code = error[0]
                error_text = error[1]

                if error_code == "SUBREDDIT_NOTALLOWED":
                    raise RedditSubredditError(
                        f"Not allowed to post in r/{subreddit}. "
                        "You may be banned or the subreddit is restricted."
                    )
                elif error_code == "SUBREDDIT_NOEXIST":
                    raise RedditSubredditError(
                        f"Subreddit r/{subreddit} does not exist"
                    )
                elif error_code == "RATELIMIT":
                    raise RedditRateLimitError(f"Posting rate limit: {error_text}")
                elif error_code == "NO_TEXT":
                    error_messages.append("Post text is required")
                elif error_code == "NO_TITLE":
                    error_messages.append("Post title is required")
                elif error_code == "TOO_LONG":
                    error_messages.append("Post content exceeds maximum length")
                elif error_code == "ALREADY_SUB":
                    error_messages.append("This link has already been submitted")
                else:
                    error_messages.append(f"{error_code}: {error_text}")
            else:
                error_messages.append(str(error))

        return "; ".join(error_messages) if error_messages else "Unknown error"

    def reply(self, post_id: str, content: str) -> PostResult:
        """
        Reply to a post or comment.

        Args:
            post_id: Full thing ID (e.g., "t3_abc123" for post, "t1_xyz789" for comment).
            content: Reply text.

        Returns:
            PostResult with success status.
        """
        if not post_id:
            return PostResult(success=False, error="Post ID is required")

        if not content:
            return PostResult(success=False, error="Reply content is required")

        # Ensure proper thing ID format
        if not post_id.startswith(("t1_", "t3_")):
            # Assume it's a post if no prefix
            post_id = f"t3_{post_id}"

        logger.info(f"Replying to {post_id}")

        try:
            data = {
                "thing_id": post_id,
                "text": content,
                "api_type": "json",
            }

            response = self._api_request("POST", "/api/comment", data=data)

            json_data = response.get("json", {})
            errors = json_data.get("errors", [])

            if errors:
                error_msg = "; ".join(str(e) for e in errors)
                logger.error(f"Reply error: {error_msg}")
                return PostResult(success=False, error=error_msg)

            data_section = json_data.get("data", {})
            things = data_section.get("things", [])

            if things:
                comment_data = things[0].get("data", {})
                comment_id = comment_data.get("id") or comment_data.get("name")
                comment_link = comment_data.get("link_permalink")

                logger.info(f"Successfully replied: {comment_id}")

                return PostResult(
                    success=True,
                    post_id=comment_id,
                    url=comment_link,
                )

            return PostResult(
                success=True,
                post_id=None,
                error="Reply posted but ID not returned",
            )

        except RedditAPIError as e:
            logger.error(f"API error replying to {post_id}: {e}")
            return PostResult(success=False, error=str(e))
        except Exception as e:
            logger.error(f"Unexpected error replying to {post_id}: {e}")
            return PostResult(success=False, error=f"Unexpected error: {e}")

    def get_mentions(self, since_id: Optional[str] = None) -> list[dict]:
        """
        Get username mentions and replies.

        Args:
            since_id: Return only mentions after this ID (for pagination).

        Returns:
            List of mention/reply dictionaries.
        """
        logger.info("Fetching mentions")

        try:
            params: dict[str, Any] = {"limit": 25}
            if since_id:
                params["after"] = since_id

            response = self._api_request(
                "GET",
                "/message/inbox",
                params=params,
            )

            data = response.get("data", {})
            children = data.get("children", [])

            mentions = []
            for child in children:
                if child.get("kind") in ("t1", "t4"):  # Comment or message
                    child_data = child.get("data", {})
                    mentions.append(
                        {
                            "id": child_data.get("name"),
                            "author": child_data.get("author"),
                            "body": child_data.get("body"),
                            "subreddit": child_data.get("subreddit"),
                            "subject": child_data.get("subject"),
                            "created_utc": child_data.get("created_utc"),
                            "parent_id": child_data.get("parent_id"),
                            "link_id": child_data.get("link_id"),
                            "is_comment": child.get("kind") == "t1",
                        }
                    )

            logger.info(f"Retrieved {len(mentions)} mentions")
            return mentions

        except RedditAPIError as e:
            logger.error(f"Error fetching mentions: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching mentions: {e}")
            return []

    def get_rate_limit_status(self) -> dict:
        """
        Get current rate limit status.

        Returns:
            Dictionary with rate limit information.
        """
        status = self.rate_limiter.get_status()
        return {
            "requests_remaining": status.requests_remaining,
            "requests_used": status.requests_used,
            "reset_timestamp": status.reset_timestamp,
            "reset_in_seconds": max(0, status.reset_timestamp - time.time()),
            "limit_per_minute": self.rate_limiter.max_requests,
        }

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def __enter__(self) -> "RedditClient":
        """Context manager entry."""
        self.authenticate()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()


# -----------------------------------------------------------------------------
# Convenience Functions
# -----------------------------------------------------------------------------


def create_client(project_prefix: Optional[str] = None) -> RedditClient:
    """
    Create and authenticate a Reddit client.

    Args:
        project_prefix: Optional project prefix for env vars.

    Returns:
        Authenticated RedditClient instance.
    """
    client = RedditClient(project_prefix=project_prefix)
    client.authenticate()
    return client


def quick_post(
    subreddit: str,
    title: str,
    content: Optional[str] = None,
    link: Optional[str] = None,
    project_prefix: Optional[str] = None,
) -> PostResult:
    """
    Quick function to post to Reddit.

    Args:
        subreddit: Target subreddit.
        title: Post title.
        content: Text content (for self posts).
        link: URL (for link posts).
        project_prefix: Optional project prefix for env vars.

    Returns:
        PostResult with success status.
    """
    with RedditClient(project_prefix=project_prefix) as client:
        return client.post_to_subreddit(
            subreddit=subreddit,
            title=title,
            content=content,
            link=link,
        )


# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Reddit Client CLI")
    parser.add_argument("--auth-test", action="store_true", help="Test authentication")
    parser.add_argument("--subreddit", help="Target subreddit")
    parser.add_argument("--title", help="Post title")
    parser.add_argument("--content", help="Post content")
    parser.add_argument("--link", help="Link URL for link posts")
    parser.add_argument("--reply-to", help="Thing ID to reply to")
    parser.add_argument("--mentions", action="store_true", help="Fetch mentions")
    parser.add_argument(
        "--rate-limit", action="store_true", help="Show rate limit status"
    )
    parser.add_argument("--prefix", help="Project prefix for env vars")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        client = RedditClient(project_prefix=args.prefix)

        if args.auth_test:
            print("Testing authentication...")
            client.authenticate()
            print("Authentication successful!")
            return

        if args.rate_limit:
            status = client.get_rate_limit_status()
            print(f"Rate limit status: {status}")
            return

        if args.mentions:
            client.authenticate()
            mentions = client.get_mentions()
            print(f"Found {len(mentions)} mentions:")
            for m in mentions:
                print(f"  - {m.get('author')}: {m.get('body', '')[:50]}...")
            return

        if args.reply_to:
            if not args.content:
                print("Error: --content required for replies")
                return
            client.authenticate()
            result = client.reply(args.reply_to, args.content)
            print(f"Reply result: {result}")
            return

        if args.subreddit and args.title:
            result = quick_post(
                subreddit=args.subreddit,
                title=args.title,
                content=args.content,
                link=args.link,
                project_prefix=args.prefix,
            )
            print(f"Post result: {result}")
            return

        parser.print_help()

    except RedditAuthError as e:
        print(f"Authentication error: {e}")
    except RedditAPIError as e:
        print(f"API error: {e}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
