"""Range parsing and the proxy's constants: `python test_range.py`."""
from cdisc_backend.routes import audio

CASES = [
    (None, None),
    ("", None),
    ("bytes=0-", (0, None)),
    ("bytes=0-99", (0, 99)),
    ("bytes=500-", (500, None)),
    ("bytes=100-200", (100, 200)),
    # Suffix ranges ("the last 500 bytes") are not understood: whole file.
    ("bytes=-500", None),
    ("items=0-10", None),
    ("bytes=abc-", None),
    ("bytes=10-x", (10, None)),
]

passed = 0
for header, expected in CASES:
    got = audio._requested_range(header)
    assert got == expected, (header, got, expected)
    passed += 1
    print("ok  %-16r -> %r" % (header, got))

assert "Range" in audio.FORWARD and "Cookie" not in audio.FORWARD
assert "Content-Range" in audio.RETURN and "Set-Cookie" not in audio.RETURN
assert audio.HOLD_AT_MOST == 32 * 1024 * 1024
assert audio.AHEAD == 1024 * 1024
passed += 4
print(f"\n{passed} passed")
