"""PDFium worker for app/filters/pdf_filter.py (#808).

Run as a SCRIPT in a child process (`python -I _pdf_worker.py <memory_mb> <cpu_s>`), never
imported by the app at start up: the parent talks to it over stdin/stdout with length
prefixed frames (an 8-byte big-endian length, then that many bytes). A command is one JSON
frame; the "open" command is followed by one raw frame holding the PDF. Every reply is one
JSON frame; an "ext" reply with a render is followed by one raw PNG frame.

The parent enforces a wall-clock limit per call and kills this process when PDFium hangs
(a form that draws the next form four times, a page-tree DAG), so a hostile file can hold
neither an app worker nor the in-process lock. On Linux and macOS the worker also sets an
address-space and a CPU limit; Windows has the parent's kill only.

PdfSession is also used in-process (config.PDF_ISOLATE=0, tests that spy on pypdfium2).
Only the standard library, pypdfium2 and (for a render) Pillow are imported here."""

import contextlib
import io
import json
import os
import struct
import sys

MAX_FRAME = 1 << 31


class PdfSession:
    """One open PDF. Every method mirrors one wire command."""

    def __init__(self, data: bytes):
        import pypdfium2 as pdfium

        self._pdfium = pdfium
        self.pdf = pdfium.PdfDocument(data)
        self.npages = len(self.pdf)

    def preflight(self, pno: int, objects_left: int) -> dict:
        """Object budget first (it is free), then the page text and size."""
        page = self.pdf[pno]
        try:
            count = self._pdfium.raw.FPDFPage_CountObjects(page.raw)
            if objects_left - count < 0:
                return {"ok": True, "over": True, "objs": count}
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range() or ""
            finally:
                textpage.close()
            width, height = page.get_size()
            return {"ok": True, "objs": count, "text": text, "w": width, "h": height}
        finally:
            page.close()

    def extract(self, pno: int, scale) -> tuple[dict, bytes | None]:
        """The page text and, when `scale` is not None, the page as a PNG."""
        page = self.pdf[pno]
        try:
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range() or ""
            finally:
                textpage.close()
            png = None
            if scale is not None:
                bitmap = page.render(scale=scale)
                try:
                    buf = io.BytesIO()
                    bitmap.to_pil().save(buf, format="PNG")
                    png = buf.getvalue()
                finally:
                    bitmap.close()
            return {"ok": True, "text": text, "png": png is not None}, png
        finally:
            page.close()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.pdf.close()


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def read_frame(stream) -> bytes:
    (n,) = struct.unpack(">Q", _read_exact(stream, 8))
    if n > MAX_FRAME:
        raise ValueError("frame too large")
    return _read_exact(stream, n)


def write_frame(stream, payload: bytes) -> None:
    stream.write(struct.pack(">Q", len(payload)))
    stream.write(payload)


def _limits(memory_mb: int, cpu_s: int) -> None:
    try:
        import resource
    except ImportError:  # Windows: the parent's wall-clock kill is the limit
        return
    # A hostile file spins a core until the kill: let the app's own threads go first.
    with contextlib.suppress(OSError):
        os.nice(10)
    for which, value in (
        (getattr(resource, "RLIMIT_AS", None), memory_mb * 1024 * 1024),
        (getattr(resource, "RLIMIT_CPU", None), cpu_s),
    ):
        if which is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (value, value))


def main() -> None:
    memory_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 1024
    cpu_s = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    # Keep stdout for the protocol only: anything PDFium prints goes to stderr.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    inp = sys.stdin.buffer
    _limits(memory_mb, cpu_s)
    session = None

    def reply(obj: dict, raw: bytes | None = None) -> None:
        write_frame(out, json.dumps(obj).encode("utf-8"))
        if raw is not None:
            write_frame(out, raw)
        out.flush()

    while True:
        try:
            cmd = json.loads(read_frame(inp).decode("utf-8"))
        except EOFError:
            break
        try:
            op = cmd["op"]
            if op == "open":
                data = read_frame(inp)
                session = PdfSession(data)
                reply({"ok": True, "npages": session.npages})
            elif op == "pre":
                reply(session.preflight(int(cmd["pno"]), int(cmd["left"])))
            elif op == "ext":
                meta, png = session.extract(int(cmd["pno"]), cmd.get("scale"))
                reply(meta, png)
            elif op == "close":
                if session is not None:
                    session.close()
                reply({"ok": True})
                break
            else:
                reply({"ok": False, "etype": "ValueError", "msg": "unknown command"})
        except MemoryError:
            reply({"ok": False, "etype": "MemoryError", "msg": "out of memory"})
        except Exception as exc:  # PdfiumError and anything else: the parent re-raises
            reply({"ok": False, "etype": type(exc).__name__, "msg": str(exc)[:500]})


if __name__ == "__main__":
    main()
