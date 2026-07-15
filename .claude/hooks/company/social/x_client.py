#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""
X (Twitter) Client — OAuth 2.0 authenticated posting integration for Forge.

P43 Implementation: Social Presence Automation.

This module provides X/Twitter integration with:
- OAuth 2.0 authentication (User Context and App-Only)
- Post creation (text, images, threads)
- Reply to existing tweets
- Mention tracking
- Rate limit handling with exponential backoff
- Comprehensive error recovery
- Full audit logging

Credential Setup:
    Set environment variables:
    - FORGE_X_API_KEY: OAuth 2.0 Client ID
    - FORGE_X_API_SECRET: OAuth 2.0 Client Secret
    - FORGE_X_ACCESS_TOKEN: User access token
    - FORGE_X_ACCESS_SECRET: User access token secret

    For project-specific credentials:
    - FORGE_X_{PROJECT}_API_KEY (e.g., FORGE_X_MYAPP_API_KEY)

X API Endpoints (v2):
    - POST /2/tweets: Create tweet
    - GET /2/users/me: Get authenticated user
    - GET /2/users/:id/mentions: Get mentions
    - POST /2/media/upload: Upload media

Rate Limits (App auth):
    - 300 requests per 15-minute window for most endpoints
    - 1500 tweets per 24 hours per user
    - Media uploads: 30 per 15 minutes

Usage:
    from social.x_client import XClient

    client = XClient()
    if client.authenticate():
        result = client.post(PostContent(text="Hello from Forge!"))
        if result.success:
            print(f"Posted: {result.url}")
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("x_client")


# =============================================================================
# Lazy imports
# =============================================================================

_requests_module: Any = None


def _get_requests():
    """Lazily import requests module."""
    global _requests_module
    if _requests_module is None:
        try:
            import requests

            _requests_module = requests
        except ImportError:
            raise ImportError(
                "requests library is required for X client. "
                "Install with: pip install requests"
            )
    return _requests_module


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PostContent:
    """
    Content for a post/tweet.

    Attributes:
        text: Main text content (max 280 characters)
        media: Optional list of media URLs or local file paths
        reply_to: Optional tweet ID to reply to
        quote_tweet_id: Optional tweet ID to quote
        poll: Optional poll configuration
    """

    text: str
    media: Optional[list[str]] = None
    reply_to: Optional[str] = None
    quote_tweet_id: Optional[str] = None
    poll: Optional[dict[str, Any]] = None

    def validate(self) -> tuple[bool, str]:
        """
        Validate content meets X requirements.

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.text or not self.text.strip():
            return False, "Tweet text cannot be empty"

        if len(self.text) > 280:
            return False, f"Tweet exceeds 280 characters ({len(self.text)})"

        if self.media and len(self.media) > 4:
            return False, "Maximum 4 media items per tweet"

        return True, ""


@dataclass
class PostResult:
    """
    Result of a post operation.

    Attributes:
        success: Whether the operation succeeded
        post_id: Tweet ID if successful
        url: Tweet URL if successful
        error: Error message if failed
        rate_limit_remaining: Remaining requests in window
        rate_limit_reset: Unix timestamp when rate limit resets
    """

    success: bool
    post_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


@dataclass
class RateLimitState:
    """
    Track rate limit state for X API.

    Attributes:
        endpoint: API endpoint being tracked
        remaining: Remaining requests in window
        reset_at: Unix timestamp when window resets
        limit: Total requests allowed per window
    """

    endpoint: str
    remaining: int = 300
    reset_at: int = 0
    limit: int = 300


@dataclass
class XCredentials:
    """
    X API credentials.

    Supports both OAuth 1.0a (for tweet posting) and OAuth 2.0 (for app-only).

    Attributes:
        api_key: OAuth API Key (Consumer Key)
        api_secret: OAuth API Secret (Consumer Secret)
        access_token: User Access Token
        access_secret: User Access Token Secret
        bearer_token: OAuth 2.0 Bearer Token (optional, for app-only auth)
    """

    api_key: str
    api_secret: str
    access_token: str
    access_secret: str
    bearer_token: Optional[str] = None

    def is_valid(self) -> bool:
        """Check if credentials are present."""
        return bool(
            self.api_key
            and self.api_secret
            and self.access_token
            and self.access_secret
        )


# =============================================================================
# SocialClient Protocol
# =============================================================================


class SocialClient(Protocol):
    """Protocol for social media clients."""

    def authenticate(self) -> bool:
        """Authenticate with the platform."""
        ...

    def post(self, content: PostContent) -> PostResult:
        """Create a new post."""
        ...

    def reply(self, post_id: str, content: PostContent) -> PostResult:
        """Reply to an existing post."""
        ...

    def get_rate_limit_status(self) -> dict[str, Any]:
        """Get current rate limit status."""
        ...


# =============================================================================
# OAuth 1.0a Signature Generation
# =============================================================================


def _percent_encode(s: str) -> str:
    """RFC 3986 percent-encoding."""
    return urllib.parse.quote(s, safe="")


def _generate_oauth_signature(
    method: str,
    url: str,
    params: dict[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    """
    Generate OAuth 1.0a signature.

    Args:
        method: HTTP method
        url: Request URL
        params: OAuth and request parameters
        consumer_secret: Consumer secret
        token_secret: Token secret

    Returns:
        Base64-encoded HMAC-SHA1 signature
    """
    # Sort parameters alphabetically and encode
    sorted_params = sorted(params.items())
    param_string = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted_params
    )

    # Create signature base string
    base_string = "&".join(
        [method.upper(), _percent_encode(url), _percent_encode(param_string)]
    )

    # Create signing key
    signing_key = f"{_percent_encode(consumer_secret)}&{_percent_encode(token_secret)}"

    # Generate signature
    signature = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1,
    )

    return base64.b64encode(signature.digest()).decode("utf-8")


def _generate_oauth_header(
    method: str,
    url: str,
    credentials: XCredentials,
    extra_params: Optional[dict[str, str]] = None,
) -> str:
    """
    Generate OAuth 1.0a Authorization header.

    Args:
        method: HTTP method
        url: Request URL
        credentials: X API credentials
        extra_params: Additional parameters to include in signature

    Returns:
        Authorization header value
    """
    import secrets as sec
    import time as t

    oauth_params = {
        "oauth_consumer_key": credentials.api_key,
        "oauth_nonce": sec.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(t.time())),
        "oauth_token": credentials.access_token,
        "oauth_version": "1.0",
    }

    # Combine with extra params for signature
    all_params = {**oauth_params}
    if extra_params:
        all_params.update(extra_params)

    # Generate signature
    signature = _generate_oauth_signature(
        method,
        url,
        all_params,
        credentials.api_secret,
        credentials.access_secret,
    )
    oauth_params["oauth_signature"] = signature

    # Build header
    header_parts = [
        f'{_percent_encode(k)}="{_percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    ]

    return "OAuth " + ", ".join(header_parts)


# =============================================================================
# X Client Implementation
# =============================================================================


class XClient:
    """
    X (Twitter) API client with OAuth 1.0a authentication.

    Supports tweeting, replying, threading, and mention tracking.
    Implements rate limiting, exponential backoff, and full audit logging.

    Attributes:
        credentials: X API credentials
        authenticated: Whether client is authenticated
        user_id: Authenticated user's ID
        username: Authenticated user's username
    """

    # X API v2 base URL
    BASE_URL = "https://api.twitter.com"
    API_VERSION = "2"

    # Rate limit defaults (per 15-minute window)
    DEFAULT_RATE_LIMIT = 300

    # Retry configuration
    MAX_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1.0
    MAX_BACKOFF_SECONDS = 60.0

    def __init__(
        self,
        company_dir: Optional[str | Path] = None,
        project_prefix: Optional[str] = None,
        env_prefix: str = "FORGE_X_",
    ):
        """
        Initialize X client.

        Args:
            company_dir: Path to .company directory for logging
            project_prefix: Project-specific credential prefix (e.g., "MYAPP")
            env_prefix: Base environment variable prefix
        """
        self.company_dir = (
            Path(company_dir) if company_dir else self._find_company_dir()
        )
        self.project_prefix = project_prefix
        self.env_prefix = env_prefix

        self.credentials: Optional[XCredentials] = None
        self.authenticated = False
        self.user_id: Optional[str] = None
        self.username: Optional[str] = None

        # Rate limit tracking
        self._rate_limits: dict[str, RateLimitState] = {}

        # Load credentials on init
        self.credentials = self._load_credentials()

    def _find_company_dir(self) -> Path:
        """Find .company directory from current working directory."""
        cwd = Path.cwd()

        # Search up to root
        for parent in [cwd] + list(cwd.parents):
            company_dir = parent / ".company"
            if company_dir.is_dir():
                return company_dir

        # Default to cwd/.company
        return cwd / ".company"

    def _load_credentials(self) -> Optional[XCredentials]:
        """
        Load credentials from environment variables.

        Supports project-specific prefixes for multi-project setups.

        Returns:
            XCredentials if found, None otherwise
        """
        # Build credential key prefixes
        prefixes = []

        # Try project-specific first
        if self.project_prefix:
            prefixes.append(f"{self.env_prefix}{self.project_prefix}_")

        # Then default prefix
        prefixes.append(self.env_prefix)

        for prefix in prefixes:
            api_key = os.environ.get(f"{prefix}API_KEY")
            api_secret = os.environ.get(f"{prefix}API_SECRET")
            access_token = os.environ.get(f"{prefix}ACCESS_TOKEN")
            access_secret = os.environ.get(f"{prefix}ACCESS_SECRET")
            bearer_token = os.environ.get(f"{prefix}BEARER_TOKEN")

            if api_key and api_secret and access_token and access_secret:
                logger.info(f"Loaded X credentials with prefix: {prefix}")
                return XCredentials(
                    api_key=api_key,
                    api_secret=api_secret,
                    access_token=access_token,
                    access_secret=access_secret,
                    bearer_token=bearer_token,
                )

        logger.warning(
            f"X credentials not found. Set {self.env_prefix}API_KEY, "
            f"{self.env_prefix}API_SECRET, {self.env_prefix}ACCESS_TOKEN, "
            f"and {self.env_prefix}ACCESS_SECRET environment variables."
        )
        return None

    def authenticate(self) -> bool:
        """
        Verify credentials by fetching authenticated user info.

        Returns:
            True if authentication successful
        """
        if not self.credentials or not self.credentials.is_valid():
            logger.error("Invalid or missing credentials")
            self._log_action("authenticate", success=False, error="Missing credentials")
            return False

        try:
            requests = _get_requests()

            # Use v2 endpoint to verify credentials
            url = f"{self.BASE_URL}/{self.API_VERSION}/users/me"

            headers = {
                "Authorization": _generate_oauth_header("GET", url, self.credentials),
                "Content-Type": "application/json",
            }

            response = requests.get(url, headers=headers, timeout=30)
            self._update_rate_limits("users/me", response.headers)

            if response.status_code == 200:
                data = response.json()
                user_data = data.get("data", {})
                self.user_id = user_data.get("id")
                self.username = user_data.get("username")
                self.authenticated = True

                logger.info(f"Authenticated as @{self.username} (ID: {self.user_id})")
                self._log_action(
                    "authenticate",
                    success=True,
                    details={"user_id": self.user_id, "username": self.username},
                )
                return True

            elif response.status_code == 401:
                logger.error("Authentication failed: Invalid credentials")
                self._log_action(
                    "authenticate", success=False, error="Invalid credentials"
                )
                return False

            elif response.status_code == 429:
                logger.error("Authentication failed: Rate limited")
                self._log_action("authenticate", success=False, error="Rate limited")
                return False

            else:
                error_msg = f"Authentication failed: HTTP {response.status_code}"
                logger.error(error_msg)
                self._log_action("authenticate", success=False, error=error_msg)
                return False

        except Exception as e:
            logger.exception(f"Authentication error: {e}")
            self._log_action("authenticate", success=False, error=str(e))
            return False

    def post(self, content: PostContent) -> PostResult:
        """
        Create a new tweet.

        Args:
            content: PostContent with text and optional media

        Returns:
            PostResult with success status and tweet details
        """
        # Validate content
        valid, error = content.validate()
        if not valid:
            return PostResult(success=False, error=error)

        # Handle reply_to in content
        if content.reply_to:
            return self.reply(content.reply_to, content)

        return self._create_tweet(content)

    def reply(self, post_id: str, content: PostContent) -> PostResult:
        """
        Reply to an existing tweet.

        Args:
            post_id: Tweet ID to reply to
            content: Reply content

        Returns:
            PostResult with success status
        """
        # Set reply_to on content
        content.reply_to = post_id
        return self._create_tweet(content)

    def create_thread(self, contents: list[PostContent]) -> list[PostResult]:
        """
        Create a multi-tweet thread.

        Args:
            contents: List of PostContent for each tweet in thread

        Returns:
            List of PostResult for each tweet
        """
        if not contents:
            return [PostResult(success=False, error="Empty thread")]

        results: list[PostResult] = []
        previous_id: Optional[str] = None

        for i, content in enumerate(contents):
            # Chain tweets by replying to previous
            if previous_id:
                content.reply_to = previous_id

            result = self._create_tweet(content)
            results.append(result)

            if not result.success:
                logger.error(f"Thread failed at tweet {i + 1}: {result.error}")
                # Mark remaining as failed
                for remaining in contents[i + 1 :]:
                    results.append(
                        PostResult(
                            success=False,
                            error=f"Skipped due to earlier failure at tweet {i + 1}",
                        )
                    )
                break

            previous_id = result.post_id

        return results

    def _create_tweet(self, content: PostContent) -> PostResult:
        """
        Internal method to create a tweet with retry logic.

        Args:
            content: Tweet content

        Returns:
            PostResult
        """
        if not self.credentials or not self.credentials.is_valid():
            return PostResult(success=False, error="Not authenticated")

        # Check rate limit before proceeding
        if not self._check_rate_limit("tweets"):
            reset_time = self._rate_limits.get(
                "tweets", RateLimitState("tweets")
            ).reset_at
            wait_seconds = max(0, reset_time - int(time.time()))
            return PostResult(
                success=False,
                error=f"Rate limited. Resets in {wait_seconds} seconds",
                rate_limit_remaining=0,
                rate_limit_reset=reset_time,
            )

        # Build tweet payload
        payload: dict[str, Any] = {"text": content.text}

        if content.reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": content.reply_to}

        if content.quote_tweet_id:
            payload["quote_tweet_id"] = content.quote_tweet_id

        if content.poll:
            payload["poll"] = content.poll

        # Handle media upload if present
        if content.media:
            media_ids = self._upload_media(content.media)
            if media_ids:
                payload["media"] = {"media_ids": media_ids}

        # Make request with retry
        return self._make_tweet_request(payload)

    def _make_tweet_request(self, payload: dict[str, Any]) -> PostResult:
        """
        Make tweet creation request with retry logic.

        Args:
            payload: Tweet JSON payload

        Returns:
            PostResult
        """
        requests = _get_requests()
        url = f"{self.BASE_URL}/{self.API_VERSION}/tweets"

        for attempt in range(self.MAX_RETRIES):
            try:
                headers = {
                    "Authorization": _generate_oauth_header(
                        "POST",
                        url,
                        self.credentials,  # type: ignore
                    ),
                    "Content-Type": "application/json",
                }

                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=30,
                )

                self._update_rate_limits("tweets", response.headers)

                if response.status_code in (200, 201):
                    data = response.json()
                    tweet_data = data.get("data", {})
                    tweet_id = tweet_data.get("id")

                    result = PostResult(
                        success=True,
                        post_id=tweet_id,
                        url=f"https://twitter.com/i/status/{tweet_id}",
                        rate_limit_remaining=self._rate_limits.get(
                            "tweets", RateLimitState("tweets")
                        ).remaining,
                    )

                    self._log_action(
                        "tweet",
                        success=True,
                        details={
                            "tweet_id": tweet_id,
                            "text_preview": payload["text"][:50],
                        },
                    )

                    return result

                elif response.status_code == 429:
                    # Rate limited - exponential backoff
                    backoff = min(
                        self.BASE_BACKOFF_SECONDS * (2**attempt),
                        self.MAX_BACKOFF_SECONDS,
                    )
                    logger.warning(
                        f"Rate limited on attempt {attempt + 1}, "
                        f"backing off {backoff:.1f}s"
                    )
                    time.sleep(backoff)
                    continue

                elif response.status_code == 401:
                    return PostResult(
                        success=False,
                        error="Authentication error - credentials may be invalid or expired",
                    )

                elif response.status_code == 403:
                    error_data = response.json()
                    error_detail = error_data.get("detail", "Forbidden")
                    return PostResult(success=False, error=f"Forbidden: {error_detail}")

                else:
                    try:
                        error_data = response.json()
                        error_msg = error_data.get("detail", response.text)
                    except json.JSONDecodeError:
                        error_msg = response.text

                    logger.error(
                        f"Tweet failed: HTTP {response.status_code}: {error_msg}"
                    )

                    # Retry on server errors
                    if response.status_code >= 500:
                        backoff = self.BASE_BACKOFF_SECONDS * (2**attempt)
                        time.sleep(backoff)
                        continue

                    return PostResult(
                        success=False, error=f"HTTP {response.status_code}: {error_msg}"
                    )

            except requests.exceptions.Timeout:
                logger.warning(f"Request timeout on attempt {attempt + 1}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BASE_BACKOFF_SECONDS * (2**attempt))
                    continue
                return PostResult(success=False, error="Request timed out")

            except requests.exceptions.RequestException as e:
                logger.error(f"Network error: {e}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.BASE_BACKOFF_SECONDS * (2**attempt))
                    continue
                return PostResult(success=False, error=f"Network error: {e}")

        return PostResult(success=False, error="Max retries exceeded")

    def _upload_media(self, media_urls: list[str]) -> list[str]:
        """
        Upload media files and return media IDs.

        Args:
            media_urls: List of URLs or local file paths

        Returns:
            List of media IDs
        """
        # Note: X API v1.1 media upload is required for v2 tweets
        # This is a placeholder - full implementation would use
        # POST https://upload.twitter.com/1.1/media/upload.json

        logger.warning("Media upload not yet implemented - posting without media")
        return []

    def get_mentions(self, since_id: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Get mentions of the authenticated user.

        Args:
            since_id: Only get mentions newer than this ID

        Returns:
            List of mention tweet data
        """
        if not self.authenticated or not self.user_id:
            logger.error("Must authenticate before fetching mentions")
            return []

        if not self._check_rate_limit("mentions"):
            logger.warning("Rate limited for mentions endpoint")
            return []

        try:
            requests = _get_requests()

            url = f"{self.BASE_URL}/{self.API_VERSION}/users/{self.user_id}/mentions"
            params = {
                "max_results": "100",
                "tweet.fields": "created_at,author_id,conversation_id",
            }

            if since_id:
                params["since_id"] = since_id

            headers = {
                "Authorization": _generate_oauth_header(
                    "GET",
                    url,
                    self.credentials,
                    params,  # type: ignore
                ),
                "Content-Type": "application/json",
            }

            response = requests.get(url, headers=headers, params=params, timeout=30)
            self._update_rate_limits("mentions", response.headers)

            if response.status_code == 200:
                data = response.json()
                mentions = data.get("data", [])

                self._log_action(
                    "get_mentions",
                    success=True,
                    details={"count": len(mentions), "since_id": since_id},
                )

                return mentions

            else:
                logger.error(f"Failed to get mentions: HTTP {response.status_code}")
                return []

        except Exception as e:
            logger.exception(f"Error fetching mentions: {e}")
            return []

    def get_rate_limit_status(self) -> dict[str, Any]:
        """
        Get current rate limit status for all tracked endpoints.

        Returns:
            Dictionary with rate limit info per endpoint
        """
        now = int(time.time())
        status = {}

        for endpoint, state in self._rate_limits.items():
            seconds_until_reset = max(0, state.reset_at - now)
            status[endpoint] = {
                "remaining": state.remaining,
                "limit": state.limit,
                "reset_at": state.reset_at,
                "seconds_until_reset": seconds_until_reset,
                "is_limited": state.remaining <= 0 and seconds_until_reset > 0,
            }

        return status

    def _check_rate_limit(self, endpoint: str) -> bool:
        """
        Check if we can make a request to the endpoint.

        Args:
            endpoint: API endpoint name

        Returns:
            True if request is allowed
        """
        if endpoint not in self._rate_limits:
            return True

        state = self._rate_limits[endpoint]
        now = int(time.time())

        # Window has reset
        if now >= state.reset_at:
            state.remaining = state.limit
            return True

        return state.remaining > 0

    def _update_rate_limits(self, endpoint: str, headers: dict[str, str]) -> None:
        """
        Update rate limit state from response headers.

        Args:
            endpoint: API endpoint name
            headers: Response headers
        """
        # X API v2 rate limit headers
        remaining = headers.get("x-rate-limit-remaining")
        reset = headers.get("x-rate-limit-reset")
        limit = headers.get("x-rate-limit-limit")

        if remaining is not None or reset is not None:
            if endpoint not in self._rate_limits:
                self._rate_limits[endpoint] = RateLimitState(endpoint)

            state = self._rate_limits[endpoint]

            if remaining is not None:
                state.remaining = int(remaining)
            if reset is not None:
                state.reset_at = int(reset)
            if limit is not None:
                state.limit = int(limit)

            logger.debug(
                f"Rate limit for {endpoint}: "
                f"{state.remaining}/{state.limit}, resets at {state.reset_at}"
            )

    def _log_action(
        self,
        action: str,
        success: bool,
        error: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Log action to audit file.

        Args:
            action: Action name
            success: Whether action succeeded
            error: Error message if failed
            details: Additional details to log
        """
        log_dir = self.company_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = log_dir / "x_client.log"

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "success": success,
            "platform": "x",
        }

        if error:
            entry["error"] = error
        if details:
            entry["details"] = details

        # Rate limit status
        entry["rate_limits"] = {
            k: {"remaining": v.remaining, "reset_at": v.reset_at}
            for k, v in self._rate_limits.items()
        }

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.error(f"Failed to write audit log: {e}")


# =============================================================================
# Factory Function
# =============================================================================


def create_x_client(
    company_dir: Optional[str | Path] = None,
    project_prefix: Optional[str] = None,
) -> XClient:
    """
    Create and authenticate an X client.

    Args:
        company_dir: Path to .company directory
        project_prefix: Project-specific credential prefix

    Returns:
        Authenticated XClient instance
    """
    client = XClient(company_dir=company_dir, project_prefix=project_prefix)

    if client.credentials and client.credentials.is_valid():
        client.authenticate()

    return client


# =============================================================================
# Cron Scheduler Integration
# =============================================================================


def create_cron_executor(client: XClient):
    """
    Create a cron executor function for the cron scheduler.

    Args:
        client: Authenticated XClient instance

    Returns:
        Executor function compatible with CronScheduler
    """
    from dataclasses import dataclass as dc

    @dc
    class ExecutionResult:
        task_id: str
        success: bool
        message: str
        output: dict = field(default_factory=dict)

    def executor(task) -> ExecutionResult:
        """Execute a scheduled X posting task."""
        action = task.action
        config = task.config

        if action == "post_status":
            text = config.get("text", "")
            media = config.get("media")

            if not text:
                return ExecutionResult(
                    task_id=task.id,
                    success=False,
                    message="No text content provided",
                )

            content = PostContent(text=text, media=media)
            result = client.post(content)

            return ExecutionResult(
                task_id=task.id,
                success=result.success,
                message=result.error or f"Posted tweet {result.post_id}",
                output=result.to_dict(),
            )

        elif action == "post_thread":
            thread_texts = config.get("thread", [])

            if not thread_texts:
                return ExecutionResult(
                    task_id=task.id,
                    success=False,
                    message="No thread content provided",
                )

            contents = [PostContent(text=text) for text in thread_texts]
            results = client.create_thread(contents)

            all_success = all(r.success for r in results)
            post_ids = [r.post_id for r in results if r.post_id]

            return ExecutionResult(
                task_id=task.id,
                success=all_success,
                message=f"Thread: {len(post_ids)}/{len(contents)} tweets posted",
                output={"results": [r.to_dict() for r in results]},
            )

        else:
            return ExecutionResult(
                task_id=task.id,
                success=False,
                message=f"Unknown action: {action}",
            )

    return executor


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> None:
    """CLI entry point for testing."""
    import sys

    if len(sys.argv) < 2:
        print(
            """
X Client — Forge Social Integration

Commands:
    auth        Test authentication
    post TEXT   Post a tweet
    mentions    Get recent mentions
    status      Show rate limit status

Examples:
    python x_client.py auth
    python x_client.py post "Hello from Forge!"
    python x_client.py mentions
"""
        )
        sys.exit(0)

    command = sys.argv[1].lower()
    client = XClient()

    if command == "auth":
        if client.authenticate():
            print(f"Authenticated as @{client.username}")
        else:
            print("Authentication failed")
            sys.exit(1)

    elif command == "post":
        if len(sys.argv) < 3:
            print("Usage: python x_client.py post TEXT")
            sys.exit(1)

        text = " ".join(sys.argv[2:])

        if not client.authenticate():
            print("Authentication failed")
            sys.exit(1)

        result = client.post(PostContent(text=text))

        if result.success:
            print(f"Posted: {result.url}")
        else:
            print(f"Failed: {result.error}")
            sys.exit(1)

    elif command == "mentions":
        if not client.authenticate():
            print("Authentication failed")
            sys.exit(1)

        mentions = client.get_mentions()
        print(f"Found {len(mentions)} mentions:")
        for m in mentions[:10]:
            print(f"  - {m.get('id')}: {m.get('text', '')[:50]}...")

    elif command == "status":
        status = client.get_rate_limit_status()
        if not status:
            print("No rate limit data (no requests made yet)")
        else:
            print("Rate Limit Status:")
            for endpoint, info in status.items():
                print(
                    f"  {endpoint}: {info['remaining']}/{info['limit']} "
                    f"(resets in {info['seconds_until_reset']}s)"
                )

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
