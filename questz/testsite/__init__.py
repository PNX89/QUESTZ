"""The bundled target. Drift detection cannot be regression tested against a third party
site, because you cannot ask one to change its DOM on cue."""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from questz.types import QuestzError

__all__ = ["TESTSITE_ROOT", "VARIANTS", "serve", "variant_dir"]

TESTSITE_ROOT = Path(__file__).parent
VARIANTS = ("v1", "v1c", "v2", "v2i")


def variant_dir(variant: str) -> Path:
    directory = TESTSITE_ROOT / variant
    if not directory.is_dir():
        raise QuestzError(
            f"unknown test site variant {variant!r}, expected one of {list(VARIANTS)}"
        )
    return directory


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """A request log per asset would bury the journal, which is the output that matters."""


def serve(
    variant: str = "v1", *, host: str = "127.0.0.1", port: int = 0
) -> tuple[ThreadingHTTPServer, str]:
    """Bind a server and hand it back unstarted, along with its literal URL.

    ThreadingHTTPServer rather than HTTPServer because browsers pre-open sockets that a
    single threaded server waits on indefinitely, which presents as a Playwright timeout.
    Port 0 means the OS assigns a free port, so parallel CI legs cannot collide.
    """
    handler = partial(_QuietHandler, directory=str(variant_dir(variant)))
    server = ThreadingHTTPServer((host, port), handler)
    assigned = int(server.server_address[1])
    # The literal address, never "localhost", which can resolve to ::1 first and stall.
    return server, f"http://{host}:{assigned}"
