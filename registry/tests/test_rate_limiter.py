"""
Unit tests for RateLimiter.

Tests the token bucket algorithm directly — no gRPC required.
"""

import time

import pytest

from registry.servicer.registry import RateLimiter


PEER_A  = "peerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PEER_B  = "peerBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
SHARE_1 = "share111111111111111111111111111111111111111111111"
SHARE_2 = "share222222222222222222222222222222222222222222222"


def test_allows_within_rate():
    """First N requests within burst should all be allowed."""
    burst = 5
    rl = RateLimiter(rate_per_minute=10, burst=burst)

    for i in range(burst):
        allowed, retry_after = rl.is_allowed(PEER_A, SHARE_1)
        assert allowed, f"Request {i+1} should be allowed (within burst of {burst})"
        assert retry_after == 0.0


def test_blocks_when_exhausted():
    """After burst is used up, next request is denied with retry_after > 0."""
    burst = 3
    rl = RateLimiter(rate_per_minute=6, burst=burst)

    # Drain the bucket.
    for _ in range(burst):
        allowed, _ = rl.is_allowed(PEER_A, SHARE_1)
        assert allowed

    # One more should be denied.
    allowed, retry_after = rl.is_allowed(PEER_A, SHARE_1)
    assert not allowed
    assert retry_after > 0.0


def test_recovers_after_wait():
    """After enough time has passed, another request is allowed."""
    # rate = 60/min = 1/s, so one token refills per second.
    rl = RateLimiter(rate_per_minute=60, burst=1)

    # Drain the single token.
    allowed, _ = rl.is_allowed(PEER_A, SHARE_1)
    assert allowed

    # Should be denied now.
    allowed, _ = rl.is_allowed(PEER_A, SHARE_1)
    assert not allowed

    # Manually advance the bucket's last_refill_time by 1 second into the past
    # so that the next call refills 1 token without sleeping.
    key = (PEER_A, SHARE_1)
    tokens, last = rl._buckets[key]
    rl._buckets[key] = (tokens, last - 1.0)  # pretend 1 second elapsed

    allowed, retry_after = rl.is_allowed(PEER_A, SHARE_1)
    assert allowed
    assert retry_after == 0.0


def test_zero_rate_always_allows():
    """When rate_per_minute=0, is_allowed always returns True."""
    rl = RateLimiter(rate_per_minute=0)

    for _ in range(1000):
        allowed, retry_after = rl.is_allowed(PEER_A, SHARE_1)
        assert allowed
        assert retry_after == 0.0


def test_keys_isolated():
    """Different (peer_id, share_id) pairs have independent buckets."""
    burst = 2
    rl = RateLimiter(rate_per_minute=10, burst=burst)

    # Drain PEER_A / SHARE_1.
    for _ in range(burst):
        allowed, _ = rl.is_allowed(PEER_A, SHARE_1)
        assert allowed

    # PEER_A / SHARE_1 should now be rate-limited.
    allowed, retry_after = rl.is_allowed(PEER_A, SHARE_1)
    assert not allowed
    assert retry_after > 0.0

    # PEER_B / SHARE_1 should still be allowed (different peer).
    allowed, _ = rl.is_allowed(PEER_B, SHARE_1)
    assert allowed

    # PEER_A / SHARE_2 should still be allowed (different share).
    allowed, _ = rl.is_allowed(PEER_A, SHARE_2)
    assert allowed
