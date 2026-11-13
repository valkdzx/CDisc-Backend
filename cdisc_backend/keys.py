"""Keys: one per Minecraft server, issued here, counted here, revoked here.

<p>The plugin is open source, so nothing it ships with can be a secret: a key
compiled into it is a key published with it. What a key can honestly do is
name a caller. Each server asks for its own the first time it starts, keeps
it in its own config, and sends it with every request — and from then on the
limits are counted per key rather than per address.

<p>That is what makes room for a real server. A hundred players with a dozen
jukeboxes going is a dozen streams from one address, which a limit per address
has to either choke or be set so loose it stops nobody. Counted per key, a
busy server gets a busy server's allowance, and a key behaving like a
downloader is revoked by itself without anyone else noticing.

<p>Anyone can ask for a key; that is the point of a public node. What the
asking costs is a second of arithmetic (a proof of work) and one of a few
registrations a day from an address — nothing to a server that does it once,
something to a script that wants a thousand.

<p>Only a hash of each key is stored, so the file is a list of who may call
rather than a list of things worth stealing.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from .youtube.cookies import write_private

log = logging.getLogger("cdisc.keys")

PREFIX = "cd"

#: How long a challenge may be answered for. A server solves one in a second.
CHALLENGE_TTL = 300

#: Keys ever issued by one node. A ceiling rather than a quota: reached, it
#: means somebody is registering for sport, and the answer is a person looking.
MAX_KEYS = 200_000

#: How often usage is written down while the process runs.
SAVE_EVERY = 300


def _digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def leading_zero_bits(digest):
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + (8 - byte.bit_length())
    return bits


class Keys:
    """The keys this node has issued, and the challenges it has out."""

    def __init__(self, path=None, pow_bits=20, per_day=3):
        self.path = Path(path) if path else None
        self.pow_bits = pow_bits
        self.per_day = per_day
        #: id -> {"hash", "created", "address", "banned", "note", "requests", "seen"}
        self.records = {}
        #: Signs challenges. Per process: a restart voids the ones outstanding,
        #: which costs a plugin one more second and nothing else.
        self._secret = secrets.token_bytes(32)
        #: Challenges already answered, so one solution is one key.
        self._spent = {}
        #: address -> times of its recent registrations
        self._registered = {}
        self._dirty = False
        self._saved_at = time.monotonic()
        self._load()

    # ------------------------------------------------------------- storage

    def _load(self):
        if not self.path or not self.path.is_file():
            return
        try:
            self.records = json.loads(self.path.read_text(encoding="utf-8"))["keys"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Not started with an empty list on top of a file that would then
            # be overwritten: every server's key is in it.
            raise RuntimeError(f"The keys file {self.path} cannot be read: {e}")
        log.info("Keys: %d issued, %d revoked", len(self.records),
                 sum(1 for r in self.records.values() if r.get("banned")))

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".new")
            write_private(temporary, json.dumps({"keys": self.records}, indent=1))
            os.replace(temporary, self.path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError as e:
            log.warning("Could not write the keys file: %s", e)

    def maybe_save(self):
        if self._dirty and time.monotonic() - self._saved_at > SAVE_EVERY:
            self.save()

    # ---------------------------------------------------------- registering

    def challenge(self):
        """Something to do arithmetic on, signed so it cannot be made up."""
        stamp = str(int(time.time()))
        nonce = secrets.token_hex(8)
        seal = hmac.new(self._secret, f"{stamp}.{nonce}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        return {"challenge": f"{stamp}.{nonce}.{seal}", "bits": self.pow_bits,
                "expires_in": CHALLENGE_TTL,
                "how": "find a counter so that sha256(challenge + ':' + counter) "
                       "starts with `bits` zero bits"}

    def _solved(self, challenge, counter):
        try:
            stamp, nonce, seal = challenge.split(".")
            age = time.time() - int(stamp)
        except ValueError:
            return False
        expected = hmac.new(self._secret, f"{stamp}.{nonce}".encode(),
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(seal, expected) or not 0 <= age <= CHALLENGE_TTL:
            return False
        if challenge in self._spent:
            return False
        work = hashlib.sha256(f"{challenge}:{counter}".encode()).digest()
        return leading_zero_bits(work) >= self.pow_bits

    def register(self, challenge, counter, address=None, note=""):
        """
        A new key for a solved challenge.

        :param address: who is asking, or None when that cannot be known
                        honestly — then only the arithmetic stands guard
        :returns: (key, None), or (None, reason)
        """
        if not isinstance(challenge, str) or not self._solved(challenge, str(counter)[:64]):
            return None, "bad_solution"
        if len(self.records) >= MAX_KEYS:
            return None, "full"

        now = time.time()
        if address and self.per_day:
            recent = [at for at in self._registered.get(address, []) if now - at < 86400]
            if len(recent) >= self.per_day:
                return None, "too_many_registrations"
            self._registered[address] = recent + [now]
            if len(self._registered) > 8192:
                for other in [a for a, times in self._registered.items()
                              if all(now - at >= 86400 for at in times)]:
                    self._registered.pop(other, None)

        self._spent[challenge] = now
        for old in [c for c, at in self._spent.items() if now - at > CHALLENGE_TTL]:
            self._spent.pop(old, None)

        ident = secrets.token_hex(6)
        while ident in self.records:
            ident = secrets.token_hex(6)
        secret = secrets.token_urlsafe(24)
        self.records[ident] = {"hash": _digest(secret), "created": int(now),
                               "address": address or "", "banned": False,
                               "note": str(note)[:80], "requests": 0, "seen": 0}
        self.save()
        return f"{PREFIX}_{ident}_{secret}", None

    # -------------------------------------------------------------- checking

    @staticmethod
    def looks_like(presented):
        return presented.startswith(PREFIX + "_") and presented.count("_") >= 2

    def check(self, presented):
        """
        :returns: (id, None) for a good key; (None, "unknown_key") or
                  (id, "key_revoked") otherwise
        """
        try:
            _, ident, secret = presented.split("_", 2)
        except ValueError:
            return None, "unknown_key"
        record = self.records.get(ident)
        if record is None or not hmac.compare_digest(record["hash"], _digest(secret)):
            return None, "unknown_key"
        if record.get("banned"):
            return ident, "key_revoked"
        record["requests"] = record.get("requests", 0) + 1
        record["seen"] = int(time.time())
        self._dirty = True
        return ident, None

    # -------------------------------------------------------------- managing

    def revoke(self, ident, banned=True, note=None):
        record = self.records.get(ident)
        if record is None:
            return False
        record["banned"] = banned
        if note is not None:
            record["note"] = str(note)[:80]
        self.save()
        return True

    def describe(self):
        """Every key, without anything that could be used as one."""
        return [dict(id=ident, **{k: v for k, v in record.items() if k != "hash"})
                for ident, record in sorted(self.records.items(),
                                            key=lambda kv: -kv[1].get("seen", 0))]
