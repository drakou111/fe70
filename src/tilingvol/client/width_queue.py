"""Widths the volunteer asked for, shared by the parallel searches. Each search takes the first width no other
search holds and keeps it while its strip keeps filling deeper; a width leaves the list once it is ruled
out, refused by the server, given back, or timed out. When the list is empty the server picks."""

import threading

MAX_WIDTHS = 64
MAX_WIDTH = 1000


def parse_widths(text: str) -> tuple[int, ...]:
    """ "149, 150 151" -> (149, 150, 151); empty -> ()."""
    parts = text.replace(",", " ").split()
    if not all(p.isdigit() for p in parts):
        raise ValueError("widths are whole numbers, like 149, 150, 151")
    widths = tuple(int(p) for p in parts)
    if len(widths) > MAX_WIDTHS:
        raise ValueError(f"at most {MAX_WIDTHS} widths")
    if not all(1 <= w <= MAX_WIDTH for w in widths):
        raise ValueError(f"widths must be between 1 and {MAX_WIDTH}")
    if len(set(widths)) != len(widths):
        raise ValueError("a width is listed twice")
    return widths


class WidthQueue:
    def __init__(self, widths: tuple[int, ...]) -> None:
        self._widths = list(widths)
        self._held: dict[object, int] = {}
        self._lock = threading.Lock()

    def claim(self, holder: object) -> int | None:
        """The width this search holds, or the first free one; None when the list is used up."""
        with self._lock:
            if holder in self._held:
                return self._held[holder]
            taken = set(self._held.values())
            for width in self._widths:
                if width not in taken:
                    self._held[holder] = width
                    return width
            return None

    def holds(self, holder: object, width: int) -> bool:
        with self._lock:
            return self._held.get(holder) == width

    def release(self, holder: object) -> None:
        """Stop holding the width but keep it in the list (any search may take it again)."""
        with self._lock:
            self._held.pop(holder, None)

    def drop(self, holder: object) -> int | None:
        """Remove the held width from the list for good."""
        with self._lock:
            width = self._held.pop(holder, None)
            if width is not None:
                self._widths.remove(width)
            return width

    def remaining(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._widths)
