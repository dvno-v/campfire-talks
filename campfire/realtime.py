"""In-process fan-out for live events, and the presence it implies.

An open event stream is the only evidence Campfire has that someone is online,
so presence is derived from subscriptions rather than stored. Nothing here
survives a restart, and no last-seen time is recorded anywhere.
"""

import queue
import threading
from collections import Counter


class Broker:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = {}
        self._connections = Counter()

    def subscribe(self, user_id):
        """Register a stream, reporting whether it made the user newly online."""
        channel = queue.Queue(maxsize=50)
        with self._lock:
            became_online = self._connections[user_id] == 0
            self._connections[user_id] += 1
            self._clients[channel] = user_id
        return channel, became_online

    def unsubscribe(self, channel):
        """Drop a stream, reporting whether it was that user's last one.

        One person commonly has several tabs open, so going offline means the
        last stream closing, not any stream closing.
        """
        with self._lock:
            user_id = self._clients.pop(channel, None)
            if user_id is None:
                return None, False
            self._connections[user_id] -= 1
            went_offline = self._connections[user_id] <= 0
            if went_offline:
                del self._connections[user_id]
        return user_id, went_offline

    def online_user_ids(self):
        with self._lock:
            return frozenset(self._connections)

    def publish(self, event):
        with self._lock:
            clients = list(self._clients)
        for channel in clients:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass


BROKER = Broker()
