"""In-process fan-out for live events, and the presence it implies.

An open event stream is the only evidence Campfire has that someone is online,
so presence is derived from subscriptions rather than stored. Nothing here
survives a restart, and no last-seen time is recorded anywhere.
"""

import queue
import threading
from collections import Counter


class Subscription:
    """One open event stream.

    `missed` records that this stream fell behind. A client that quietly loses
    events ends up showing a transcript that is wrong without saying so, which
    is worse than telling it to re-read the channel.
    """

    def __init__(self, user_id):
        self.user_id = user_id
        self.events = queue.Queue(maxsize=50)
        self.missed = threading.Event()


class Broker:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscriptions = set()
        self._connections = Counter()

    def subscribe(self, user_id):
        """Register a stream, reporting whether it made the user newly online."""
        subscription = Subscription(user_id)
        with self._lock:
            became_online = self._connections[user_id] == 0
            self._connections[user_id] += 1
            self._subscriptions.add(subscription)
        return subscription, became_online

    def unsubscribe(self, subscription):
        """Drop a stream, reporting whether it was that user's last one.

        One person commonly has several tabs open, so going offline means the
        last stream closing, not any stream closing.
        """
        with self._lock:
            if subscription not in self._subscriptions:
                return None, False
            self._subscriptions.discard(subscription)
            user_id = subscription.user_id
            self._connections[user_id] -= 1
            went_offline = self._connections[user_id] <= 0
            if went_offline:
                del self._connections[user_id]
        return user_id, went_offline

    def online_user_ids(self):
        with self._lock:
            return frozenset(self._connections)

    def stream_counts(self):
        """Total open streams, and how many belong to each user."""
        with self._lock:
            return len(self._subscriptions), dict(self._connections)

    def publish(self, event):
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            try:
                subscription.events.put_nowait(event)
            except queue.Full:
                subscription.missed.set()


BROKER = Broker()
