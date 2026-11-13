"""A small cache that forgets: by age, and by number.

<p>Every cache here used to be a dict whose entries were checked for age only
when somebody asked for them again. An entry nobody asked for twice was never
looked at, so it was never dropped — and the keys come from callers, which
makes "never dropped" a way to fill the process's memory one made-up id at a
time. This is the same dict with both bounds enforced.
"""
import time


class Bounded:
    """Entries kept for `ttl` seconds, and no more than `capacity` of them."""

    def __init__(self, ttl, capacity=2048):
        self.ttl = ttl
        self.capacity = capacity
        self._held = {}

    def get(self, key):
        found = self._held.get(key)
        if found is None:
            return None
        if time.monotonic() - found[0] > self.ttl:
            self._held.pop(key, None)
            return None
        return found[1]

    def put(self, key, value):
        self._held[key] = (time.monotonic(), value)
        if len(self._held) > self.capacity:
            self._trim()
        return value

    def _trim(self):
        now = time.monotonic()
        for key in [k for k, (at, _) in self._held.items() if now - at > self.ttl]:
            self._held.pop(key, None)
        # Still over: the oldest go, down to three quarters, so the next
        # arrival does not pay for a trim of its own.
        excess = len(self._held) - (self.capacity * 3) // 4
        if excess > 0:
            for key in sorted(self._held, key=lambda k: self._held[k][0])[:excess]:
                self._held.pop(key, None)

    def __len__(self):
        return len(self._held)

    def clear(self):
        self._held.clear()
