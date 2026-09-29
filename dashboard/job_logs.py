"""
Reads the log files the scheduled jobs write, for the dashboard's admin view.

A scheduled task's console output goes nowhere by default, which is why
register_scheduled_tasks.ps1 redirects each task to its own file in logs/. This
module is the read side: it knows which files belong to which job, and tails
them without ever letting a request name an arbitrary path.

Deliberately a fixed catalogue rather than a directory listing. The logs/
directory also holds the SQLite databases, and "show me a file from the logs
folder" is one traversal bug away from serving those.
"""
import os
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 2000
# Read at most this much from the end of a file. A run that loops on an error
# can produce a very large log, and the page should still open.
MAX_TAIL_BYTES = 512 * 1024

# key -> (display name, file name, what writes it)
CATALOGUE = {
    "weekly_chat": (
        "Chat Insights (weekly)", "scheduled_weekly_chat.log",
        "Scheduled task BCSands-ChatInsights-Weekly"),
    "daily_run": (
        "Content Agent (daily)", "scheduled_daily_run.log",
        "Scheduled task BCSands-ContentAgent-Daily"),
    "dashboard": (
        "Dashboard", "dashboard.log",
        "Scheduled task BCSands-Dashboard (uvicorn)"),
    "ollama": (
        "Description writer", "ollama.log",
        "Scheduled task BCSands-Ollama -- the service that writes descriptions"),
    "agent_runs": (
        "Writing decisions", "agent_runs.log",
        "One line per description: who wrote it, and why it was handed on"),
    "daily_leads": (
        "Lead emails", "scheduled_daily_leads.log",
        "Scheduled task BCSands-Leads: fetches new chats, finds leads and emails the call "
        "list at 4:00 am, 9:00 am, 11:00 am, 1:00 pm and 4:00 pm"),
    "reclaim_images": (
        "Image cleanup", "scheduled_reclaim_images.log",
        "Scheduled task BCSands-ReclaimImages: verifies published images, "
        "then deletes the staged copies that landed"),
    "batch_summary": (
        "Batch summaries", "batch_run_summary.log",
        "One block per batch run: counts, escalations, elapsed"),
    "site_monitor": (
        "Site health checks", "scheduled_site_monitor.log",
        "Scheduled task BCSands-SiteMonitor: checks the online shop every five minutes"),
}


def _path(key: str) -> str:
    return os.path.join(LOG_DIR, CATALOGUE[key][1])


def catalogue() -> list[dict]:
    """Every known log, whether or not it exists yet.

    A missing file is information, not an error: no scheduled_weekly_chat.log
    means the weekly task has never successfully started, which is exactly what
    an admin checking on a job needs to see.
    """
    out = []
    for key, (label, filename, written_by) in CATALOGUE.items():
        path = _path(key)
        exists = os.path.exists(path)
        stat = os.stat(path) if exists else None
        out.append({
            "key": key,
            "label": label,
            "filename": filename,
            "written_by": written_by,
            "exists": exists,
            "size": stat.st_size if stat else 0,
            "modified": (datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                         if stat else None),
        })
    return out


_MIN_UTF16_RUN = 16  # bytes; shorter than this is noise, not a real segment


def _utf16_run(raw: bytes, start: int, big_endian: bool) -> int:
    """Length in bytes of the UTF-16 run beginning at `start`.

    Log text is ASCII, and ASCII in UTF-16 is a strict alternation: one byte
    carrying the character, one zero byte beside it. Walking that pattern finds
    where the run ends exactly, rather than guessing from how dense the zero
    bytes are across some window -- which is too coarse to spot a short header
    followed by a long plain-text tail.
    """
    i = start
    n = len(raw)
    while i + 1 < n:
        first, second = raw[i], raw[i + 1]
        ok = (first == 0 and second != 0) if big_endian else (first != 0 and second == 0)
        if not ok:
            break
        i += 2
    return i - start


def _decode(raw: bytes) -> str:
    """Bytes to text, whatever wrote them -- including a file where that
    changed halfway through.

    These logs are appended to by whatever last ran the job, and different
    runners write different encodings. The scheduled task goes through cmd,
    which appends plain bytes. Running the same job by hand from PowerShell and
    redirecting with `>>` writes UTF-16LE with a BOM.

    Do both to one file and it is genuinely mixed: a UTF-16 header followed by
    ASCII. Sniffing the BOM and decoding the lot as UTF-16 -- which is what this
    used to do -- turned every line the scheduled task wrote into CJK mojibake,
    so the log was unreadable exactly when someone was trying to work out why a
    job had misbehaved.

    So this walks the buffer and decodes each run with the encoding it was
    actually written in. Never raises: an undecodable log still has to render.
    """
    if not raw:
        return ""

    big_endian = raw.startswith(b"\xfe\xff")
    if big_endian or raw.startswith(b"\xff\xfe"):
        raw = raw[2:]

    pieces: list[str] = []
    pos = 0
    size = len(raw)
    while pos < size:
        run = _utf16_run(raw, pos, big_endian)
        if run >= _MIN_UTF16_RUN:
            pieces.append(raw[pos:pos + run].decode(
                "utf-16-be" if big_endian else "utf-16-le", errors="replace"))
            pos += run
            continue

        # Plain run: consume until a UTF-16 stretch long enough to be real
        # starts. Scanning byte by byte rather than in fixed blocks, because an
        # odd-length plain run leaves the next segment on an odd offset.
        end = pos + 1
        while end < size and _utf16_run(raw, end, big_endian) < _MIN_UTF16_RUN:
            end += 1
        pieces.append(raw[pos:end].decode("utf-8", errors="replace"))
        pos = end

    return "".join(pieces)


def tail(key: str, lines: int = DEFAULT_TAIL_LINES) -> dict:
    """Last `lines` lines of one log. Returns {"text", "truncated", "missing"}."""
    if key not in CATALOGUE:
        return {"text": "", "truncated": False, "missing": True}

    lines = max(1, min(MAX_TAIL_LINES, lines))
    path = _path(key)
    if not os.path.exists(path):
        return {"text": "", "truncated": False, "missing": True}

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_TAIL_BYTES:
            # Even offset, so a UTF-16 seek cannot land mid-character.
            f.seek((size - MAX_TAIL_BYTES) & ~1)
            f.readline()  # discard the partial line the seek landed inside
        raw = f.read()

    text = _decode(raw)
    all_lines = text.splitlines()
    kept = all_lines[-lines:]
    return {
        "text": "\n".join(kept),
        "truncated": len(kept) < len(all_lines) or size > MAX_TAIL_BYTES,
        "missing": False,
    }
