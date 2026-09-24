"""
The Redis connection and the RQ queue batch jobs travel through.

Created lazily so importing the app never needs a Redis server, and swappable
so tests can run against an in-memory one. (Named job_queue, not queue, so it
cannot be confused with the standard library module.)
"""

from typing import Optional

from redis import Redis
from rq import Queue

from app.config import BATCH_QUEUE_NAME, REDIS_URL

_connection: Optional[Redis] = None


def get_connection() -> Redis:
    global _connection
    if _connection is None:
        _connection = Redis.from_url(REDIS_URL)
    return _connection


def set_connection(connection: Optional[Redis]) -> None:
    """Replace (or, with None, reset) the shared connection. For tests."""
    global _connection
    _connection = connection


def get_queue() -> Queue:
    return Queue(BATCH_QUEUE_NAME, connection=get_connection())
