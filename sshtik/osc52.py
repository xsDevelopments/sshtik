"""OSC 52 clipboard writes from the remote side.

VTE (the terminal widget) ignores OSC 52, so tmux, vim, sshtac and friends
can't reach the desktop clipboard through it. This scanner watches the byte
stream on its way to VTE (it doesn't change it) and reports clipboard writes,
split across reads or not. Write-only: a query (`?`) is never answered, so a
remote program can't read your clipboard.
"""
import base64
import binascii

PREFIX = b"\x1b]52;"
MAX_PAYLOAD = 1 << 20  # base64 bytes (~768 KB of text); longer is dropped


class Scanner:
    def __init__(self):
        self._pending = b""
        self._discard = False  # inside an over-long sequence: skip to its end

    def feed(self, data):
        """Return [(selection, text), ...] for the writes completed in `data`.
        selection is "primary" or "clipboard"."""
        found = []
        data = self._pending + data
        self._pending = b""
        pos = 0
        while pos < len(data):
            if self._discard:
                end, size = _terminator(data, pos)
                if end < 0:
                    return found
                self._discard = False
                pos = end + size
                continue
            start = data.find(PREFIX, pos)
            if start < 0:
                self._pending = _partial_prefix(data[pos:])
                break
            body = start + len(PREFIX)
            end, size = _terminator(data, body)
            if end < 0:
                if len(data) - body > MAX_PAYLOAD:
                    self._discard = True
                else:
                    self._pending = data[start:]
                break
            write = _decode(data[body:end])
            if write:
                found.append(write)
            pos = end + size
        return found


def _terminator(data, pos):
    """Index and length of the first BEL or ST (ESC \\) at or after pos."""
    bel = data.find(b"\x07", pos)
    st = data.find(b"\x1b\\", pos)
    if bel < 0 and st < 0:
        return -1, 0
    if st < 0 or (0 <= bel < st):
        return bel, 1
    return st, 2


def _partial_prefix(tail):
    for k in range(min(len(PREFIX) - 1, len(tail)), 0, -1):
        if tail.endswith(PREFIX[:k]):
            return tail[-k:]
    return b""


def _decode(body):
    if len(body) > MAX_PAYLOAD + 16:
        return None
    sel, sep, b64 = body.partition(b";")
    if not sep or b64 in (b"", b"?"):
        return None  # malformed, clear-request, or a read query: ignore
    try:
        text = base64.b64decode(b64, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None
    if not text:
        return None
    target = "primary" if sel and b"c" not in sel and b"p" in sel else "clipboard"
    return target, text
