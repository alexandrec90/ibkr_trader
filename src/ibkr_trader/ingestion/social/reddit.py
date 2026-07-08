"""Reddit connector (PRAW) — r/wallstreetbets, r/investing, r/stocks, r/CanadianInvestor.

Setup: create a "script" app at https://www.reddit.com/prefs/apps, put the id/secret in .env.
Privacy: authors are stored as `stable_hash(username)` only. Respect Reddit Data API terms
for retention/redistribution.
"""

from ibkr_trader.config import get_settings
from ibkr_trader.ingestion.base import Connector, stable_hash


class RedditConnector(Connector):
    name = "reddit"

    def fetch(self, subreddits: list[str] | None = None, limit: int = 100, **kwargs) -> int:
        settings = get_settings()
        if not (settings.reddit_client_id and settings.reddit_client_secret):
            raise RuntimeError("REDDIT_CLIENT_ID/SECRET not set (see .env.example)")
        subreddits = subreddits or settings.subreddits

        # TODO(skeleton): with praw.Reddit(client_id=..., client_secret=..., user_agent=...):
        #   for sub in subreddits: iterate .new(limit=limit) (and consider .hot / comments),
        #   map to SocialPost rows:
        #     platform="reddit", channel=sub, external_id=submission.id,
        #     author_hash=stable_hash(str(submission.author)), score, num_comments, raw=vars-ish
        #   upsert on (platform, external_id) so re-polls update score/num_comments.
        _ = stable_hash  # referenced so linters keep the privacy helper import visible
        raise NotImplementedError("PRAW wiring not implemented yet")
