"""Reads cameras that send JPEG pictures over HTTP, without FFmpeg.

Two kinds of source are read here:

- an MJPEG stream (multipart/x-mixed-replace), what IP Webcam, DroidCam and
  most network cameras serve on /video;
- a snapshot URL that returns one picture per request (/shot.jpg, /photo.jpg),
  which is asked again a few times a second.

OpenCV's FFmpeg reader gets both wrong. When the pictures of a stream change
size it keeps returning the last picture of the first size, and it plays a
snapshot URL as a one-frame video that has ended. It also has no way to tell a
stream that is only quiet (a new picture every few seconds) from a dead one.
"""
from __future__ import annotations

import base64
import http.client
import io
import re
import select
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Any, Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

MAX_PICTURE_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
# A snapshot URL is asked for a new picture this often.
SNAPSHOT_POLL_SECONDS = 0.2
# Reads allowed after a complete picture is in hand, to pick up newer ones that
# have already arrived instead of falling behind the camera.
_DRAIN_READS = 16
# A read waits for the camera in steps this long, checking abort() in between.
_WAIT_STEP_SECONDS = 0.2
# At least the response's own buffer, so one read hands over all of it and
# "the socket has nothing" then means "nothing has arrived".
_READ_BYTES = max(1 << 20, io.DEFAULT_BUFFER_SIZE)
_JPEG_START = b"\xff\xd8"
_CONTENT_LENGTH = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)
_BOUNDARY = re.compile(r"boundary=\"?([^\";,\s]+)", re.IGNORECASE)


class NotAPictureSource(Exception):
    """The URL does not serve JPEG pictures this reader understands; FFmpeg should try it."""


def _decode(data: Optional[bytes]) -> Optional[Any]:
    if not data or cv2 is None:
        return None
    try:
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return None


class HttpPictureCapture:
    """cv2.VideoCapture stand-in for an MJPEG stream or a snapshot URL.

    Like cv2.VideoCapture it is read by one thread. abort() may be called from
    another thread to end a read that is waiting for the camera.
    """

    backend = "HTTP JPEG reader"

    def __init__(self, url: str, read_timeout: float, snapshot_interval: float = SNAPSHOT_POLL_SECONDS):
        self.url = url
        # True once the stream closed only because no picture came within the
        # read timeout: normal for a source that changes every few seconds.
        self.idle = False
        self.last_error: Optional[str] = None
        self._read_timeout = max(0.5, float(read_timeout))
        self._snapshot_interval = snapshot_interval
        self._snapshot = False
        self._conn: Optional[http.client.HTTPConnection] = None
        self._response: Optional[http.client.HTTPResponse] = None
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._scan_from = 0
        self._wait_on_socket = False
        self._delimiter = b"\r\n--"
        self._body: Optional[bytes] = None    # a snapshot fetched and not yet handed out
        self._jpeg: Optional[bytes] = None    # grabbed, not yet decoded
        self._frame: Optional[Any] = None     # the picture open() decoded
        self._next_poll = 0.0
        self._closed = False
        self._abort = threading.Event()

    @classmethod
    def open(cls, url: str, open_timeout: float, read_timeout: float) -> "HttpPictureCapture":
        """Connect and read the first picture.

        Raises NotAPictureSource when FFmpeg should try the URL instead, and
        OSError when the camera does not answer or refuses the request.
        """
        capture = cls(url, read_timeout)
        try:
            capture._request(open_timeout)
            frame = _decode(capture._next_picture(open_timeout))
        except NotAPictureSource:
            capture.release()
            raise
        except (http.client.HTTPException, ValueError) as exc:
            capture.release()
            raise NotAPictureSource(f"{type(exc).__name__}: {exc}") from exc
        except OSError:
            capture.release()
            raise
        if frame is None:
            capture.release()
            raise NotAPictureSource("the first picture could not be decoded")
        capture._frame = frame
        return capture

    # ── cv2.VideoCapture interface ────────────────────────────────────────────

    def isOpened(self) -> bool:
        return not self._closed

    def set(self, *args: Any) -> bool:
        return False

    def grab(self) -> bool:
        if self._closed:
            return False
        if self._frame is not None:
            return True
        try:
            jpeg = self._next_picture(self._read_timeout)
        except (OSError, http.client.HTTPException, ValueError, NotAPictureSource) as exc:
            self.idle = isinstance(exc, TimeoutError) and not self._snapshot and not self._abort.is_set()
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.release()
            return False
        if jpeg is None:
            self.last_error = "the camera closed the stream"
            self.release()
            return False
        self._jpeg = jpeg
        return True

    def retrieve(self) -> Tuple[bool, Optional[Any]]:
        if self._frame is not None:
            frame, self._frame = self._frame, None
            return True, frame
        jpeg, self._jpeg = self._jpeg, None
        frame = _decode(jpeg)
        return frame is not None, frame

    def read(self) -> Tuple[bool, Optional[Any]]:
        if not self.grab():
            return False, None
        return self.retrieve()

    def release(self) -> None:
        self._closed = True
        self._close_connection()

    def abort(self) -> None:
        """End a read that is waiting for the camera. Safe to call from another thread."""
        self._abort.set()
        sock = self._sock
        if sock is not None:
            # Wakes a read blocked in the socket on Linux. Windows does not, which
            # is why _read_more() waits in steps and looks at the flag.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _close_connection(self) -> None:
        response, conn = self._response, self._conn
        self._response = self._conn = self._sock = None
        for item in (response, conn):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass

    def _request(self, timeout: float, authorize: bool = False) -> None:
        """Send the GET. Leaves a stream open for reading, or a snapshot's picture in _body."""
        self._close_connection()
        parts = urllib.parse.urlsplit(self.url)
        if not parts.hostname:
            raise NotAPictureSource("the URL has no host")
        connection_type = http.client.HTTPSConnection if parts.scheme.lower() == "https" else http.client.HTTPConnection
        conn = connection_type(parts.hostname, parts.port, timeout=timeout)
        headers = {"User-Agent": "VisionServer/1.0", "Accept": "*/*", "Connection": "close"}
        if authorize:
            credentials = f"{urllib.parse.unquote(parts.username or '')}:{urllib.parse.unquote(parts.password or '')}"
            headers["Authorization"] = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", target, headers=headers)
            # A response that ends with the connection takes the socket over
            # from conn, so keep hold of it for timeouts and abort().
            sock = conn.sock
            response = conn.getresponse()
        except ssl.SSLError as exc:
            conn.close()
            raise NotAPictureSource(f"TLS: {exc}") from exc
        except http.client.HTTPException as exc:
            conn.close()
            raise NotAPictureSource(f"{type(exc).__name__}: {exc}") from exc
        except OSError:
            conn.close()
            raise
        self._conn, self._response, self._sock = conn, response, sock

        status = response.status
        if status == 401 and not authorize and parts.username \
                and (response.getheader("WWW-Authenticate") or "").lower().startswith("basic"):
            # The password is sent only once the camera asks for Basic. Digest is left to FFmpeg.
            return self._request(timeout, authorize=True)
        if status == 401 or 300 <= status < 400:
            self._close_connection()
            raise NotAPictureSource(f"HTTP {status}")
        if status != 200:
            self._close_connection()
            raise ConnectionError(f"the camera answered HTTP {status}")

        content_type = response.getheader("Content-Type") or ""
        kind = content_type.lower()
        if kind.startswith("multipart/"):
            self._snapshot = False
            boundary = _BOUNDARY.search(content_type)
            if boundary:
                # Some cameras put the leading dashes in the boundary itself.
                self._delimiter = b"--" + boundary.group(1).lstrip("-").encode("latin-1")
            self._buffer.clear()
            self._scan_from = 0
            self._wait_on_socket = False
            if sock is not None:
                sock.settimeout(self._read_timeout)
        elif kind.startswith("image/"):
            self._snapshot = True
            body = response.read(MAX_PICTURE_BYTES + 1)
            self._close_connection()
            if not body or len(body) > MAX_PICTURE_BYTES:
                raise ConnectionError("the camera sent an empty or oversized picture")
            self._body = body
            self._next_poll = time.monotonic() + self._snapshot_interval
        else:
            self._close_connection()
            raise NotAPictureSource(f"content type '{content_type or 'unknown'}'")

    def _next_picture(self, timeout: float) -> Optional[bytes]:
        """The next picture as JPEG bytes, or None when the camera ended the stream."""
        if not self._snapshot:
            return self._next_stream_picture(timeout)
        if self._body is None:
            wait = self._next_poll - time.monotonic()
            if (wait > 0 and self._abort.wait(wait)) or self._abort.is_set():
                raise ConnectionError("stopped")
            self._request(self._read_timeout)
        body, self._body = self._body, None
        return body

    def _next_stream_picture(self, timeout: float) -> Optional[bytes]:
        if self._response is None:
            return None
        deadline = time.monotonic() + timeout
        newest: Optional[bytes] = None
        extra_reads = 0
        while True:
            picture = self._take_picture()
            while picture is not None:
                newest = picture
                picture = self._take_picture()
            if newest is not None:
                if extra_reads >= _DRAIN_READS or not self._more_waiting():
                    return newest
                extra_reads += 1
            elif time.monotonic() > deadline:
                raise TimeoutError("no picture within the read timeout")
            data = self._read_more(deadline)
            if not data:
                return newest
            self._buffer += data

    def _read_more(self, deadline: float) -> bytes:
        """The next bytes of the stream, b"" once the camera closes it."""
        response = self._response
        if self._wait_on_socket:
            while not self._readable(_WAIT_STEP_SECONDS):
                if self._abort.is_set():
                    raise ConnectionError("stopped")
                if time.monotonic() > deadline:
                    raise TimeoutError("no picture within the read timeout")
        # Waiting on the socket is right only when the response holds nothing
        # back: after the first read, and not for a chunked response, whose
        # chunk headers are read ahead into its buffer.
        self._wait_on_socket = not response.chunked
        return response.read1(_READ_BYTES)

    def _readable(self, wait: float) -> bool:
        sock = self._sock
        if sock is None:
            return True  # the read reports what is wrong
        if isinstance(sock, ssl.SSLSocket) and sock.pending() > 0:
            return True
        try:
            return bool(select.select([sock], [], [], wait)[0])
        except (OSError, ValueError):
            return True

    def _more_waiting(self) -> bool:
        """True when more of the stream has already arrived."""
        return self._wait_on_socket and self._readable(0)

    def _take_picture(self) -> Optional[bytes]:
        """Cut the next complete picture out of the buffer, or None until more of it arrives."""
        buf = self._buffer
        start = buf.find(_JPEG_START)
        if start < 0:
            if len(buf) > MAX_HEADER_BYTES:
                raise ValueError("no JPEG picture in the stream")
            return None
        length = _CONTENT_LENGTH.search(buf, 0, start)
        if length:
            end = start + int(length.group(1))
            if end - start > MAX_PICTURE_BYTES:
                raise ValueError("picture larger than the limit")
            if len(buf) < end:
                return None
        else:
            # No length given: the picture runs up to the next part's boundary.
            end = buf.find(self._delimiter, max(start + 2, self._scan_from))
            if end < 0:
                if len(buf) - start > MAX_PICTURE_BYTES:
                    raise ValueError("picture larger than the limit")
                self._scan_from = max(start + 2, len(buf) - len(self._delimiter))
                return None
        picture = bytes(buf[start:end])
        del buf[:end]
        self._scan_from = 0
        return picture
