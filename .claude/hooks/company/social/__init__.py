"""
Social Media Integration — Forge social posting clients.

P43 Implementation: Social Presence Automation.

This package provides clients for posting to various social media platforms:
- X (Twitter): x_client.py — OAuth 1.0a authenticated tweeting
- Reddit: reddit_client.py — OAuth2 authenticated posting

Each client implements authentication, posting, and rate limiting
for its respective platform. Note: Each platform has its own PostContent
and PostResult classes due to platform-specific requirements.

Usage (X/Twitter):
    from social.x_client import XClient
    from social.x_client import PostContent as XPostContent

    client = XClient()
    client.authenticate()
    result = client.post(XPostContent(text="Hello from Forge!"))

Usage (Reddit):
    from social.reddit_client import RedditClient
    from social.reddit_client import PostContent as RedditPostContent

    client = RedditClient()
    client.authenticate()
    result = client.post(RedditPostContent(
        text="Hello world!",
        subreddit="test",
        title="My Post"
    ))
"""

# Reddit client exports
from .reddit_client import (
    PostContent as RedditPostContent,
)
from .reddit_client import (
    PostResult as RedditPostResult,
)
from .reddit_client import (
    RedditAPIError,
    RedditAuthError,
    RedditClient,
    RedditRateLimitError,
    RedditSubredditError,
    quick_post,
)
from .reddit_client import (
    create_client as create_reddit_client,
)

# X client exports
from .x_client import (
    PostContent as XPostContent,
)
from .x_client import (
    PostResult as XPostResult,
)
from .x_client import (
    XClient,
    XCredentials,
    create_x_client,
)

__all__ = [
    # X/Twitter
    "XClient",
    "XCredentials",
    "XPostContent",
    "XPostResult",
    "create_x_client",
    # Reddit
    "RedditClient",
    "RedditPostContent",
    "RedditPostResult",
    "RedditAPIError",
    "RedditAuthError",
    "RedditRateLimitError",
    "RedditSubredditError",
    "create_reddit_client",
    "quick_post",
]
