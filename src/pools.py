"""Named thread pools, one per class of blocking work, so a slow call cannot starve an unrelated one.

Each is a ``TimedExecutor``: a request that waits on it has the queue and worker time reported in its
``Server-Timing`` header under the pool's name.
"""

from __future__ import annotations

from src.request_timing import TimedExecutor

# Snapshot rebuilds and cache wipes. Both take the store's build lock, so at most one runs at a time; the
# second worker lets a queued wipe wait on that lock without holding up a build that has not started.
HEAVY_READ = TimedExecutor("heavy-read", 2)

# Tailscale address and certificate probes: a subprocess each, one at a time, off the serving loop.
TAILNET = TimedExecutor("tailnet", 1)
