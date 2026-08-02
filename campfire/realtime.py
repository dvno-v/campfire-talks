"""In-process fan-out for live message events."""

import queue
import threading


class Broker:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []

    def subscribe(self):
        channel = queue.Queue(maxsize=50)
        with self._lock:
            self._clients.append(channel)
        return channel

    def unsubscribe(self, channel):
        with self._lock:
            if channel in self._clients:
                self._clients.remove(channel)

    def publish(self, event):
        with self._lock:
            clients = list(self._clients)
        for channel in clients:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass


BROKER = Broker()
