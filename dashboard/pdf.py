"""
HTML to PDF, using the browser already installed on this server.

The weekly chat report is a styled HTML document -- tables, colours, the same
thing that lands in the manager's inbox. Re-typesetting that with a PDF
library would mean a second layout to keep in step with the first, and every
Windows-friendly library that lays out real HTML is a heavy install. Chrome
and Edge are both on this machine and both print exactly what they render, so
the PDF is the report, not a copy of it.

The render is deliberately sealed off. The page is written to a temporary
file, opened from disk, and DNS is pointed at nothing, so a report holding
customer names and phone numbers cannot fetch -- or leak to -- anything
remote while it is being printed.
"""
import logging
import os
import shutil
import subprocess
import tempfile

logger = logging.getLogger("dashboard.pdf")

# Chrome first, then Edge. Windows Server has Edge by default; this machine
# happens to have both.
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

TIMEOUT_SECONDS = 90


class PdfError(RuntimeError):
    """Raised with a sentence that can be shown to whoever pressed the button."""


def browser_path() -> str:
    """The browser to print with. PDF_BROWSER in .env wins, for a server where
    it lives somewhere else."""
    override = (os.environ.get("PDF_BROWSER") or "").strip('"')
    if override:
        if os.path.exists(override):
            return override
        raise PdfError(f"PDF_BROWSER is set to {override}, which is not there.")
    for candidate in BROWSERS:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise PdfError("No browser to make the PDF with. Chrome or Edge has to be installed on "
                   "this server, or PDF_BROWSER set to one in .env.")


def render(html_document: str, *, landscape: bool = False,
           timeout: int = TIMEOUT_SECONDS) -> bytes:
    """The PDF bytes for one HTML document. Raises PdfError, never silently
    returns something that is not a PDF."""
    browser = browser_path()
    workspace = tempfile.mkdtemp(prefix="report_pdf_")
    page = os.path.join(workspace, "report.html")
    out = os.path.join(workspace, "report.pdf")
    try:
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(html_document)
        command = [
            browser, "--headless=new", "--disable-gpu",
            # The service runs as SYSTEM, which has no profile of its own.
            f"--user-data-dir={os.path.join(workspace, 'profile')}",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-sync", "--hide-scrollbars",
            # Nothing is fetched while printing: the page is ours and complete,
            # and it holds customer details.
            "--host-resolver-rules=MAP * ~NOTFOUND",
            # No sandbox because this can run as SYSTEM, printing a local file
            # this dashboard produced -- not anything from the web.
            "--no-sandbox",
            "--print-to-pdf-no-header", f"--print-to-pdf={out}",
        ]
        if landscape:
            command.append("--landscape")
        command.append(_file_url(page))
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1][:200] if detail else f"exit code {result.returncode}"
            raise PdfError(f"The PDF could not be made: {reason}")
        with open(out, "rb") as handle:
            data = handle.read()
        if not data.startswith(b"%PDF"):
            raise PdfError("The PDF came out unreadable.")
        return data
    except subprocess.TimeoutExpired:
        raise PdfError(f"Making the PDF took longer than {timeout} seconds.")
    except OSError as e:
        raise PdfError(f"The PDF could not be made: {e}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _file_url(path: str) -> str:
    return "file:///" + os.path.abspath(path).replace("\\", "/")


def is_available() -> tuple[bool, str]:
    """(ok, what to say about it) for a page that offers the download."""
    try:
        return True, os.path.basename(browser_path())
    except PdfError as e:
        return False, str(e)
