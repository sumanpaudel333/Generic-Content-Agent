"""
The weekly Chat Insights job.

Fetch -> analyse -> store -> render -> email. Importable as run_weekly() so the
dashboard's "Run now" button and the scheduled task share one code path (the
content agent's daily_run.py deliberately isn't importable, which meant the
dashboard had to reassemble its steps -- not repeating that here).

Usage:
    python -m chat_insights.weekly_run [--dry-run] [--fixture path.json] [--no-email]

Schedule weekly via Windows Task Scheduler -- see
scripts/register_scheduled_tasks.ps1.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from chat_insights import analyzer, chatbase_client, db, llm, mailer, redact, reporter
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("chat_insights.weekly_run")


def load_fixture(path: str) -> list[dict]:
    """Loads conversations from a JSON file shaped like the Chatbase export
    response. Lets the whole pipeline be exercised end to end before real API
    credentials exist."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    return [chatbase_client.normalise_conversation(r) for r in raw]


def run_weekly(*, reference: datetime | None = None, send_email: bool = True,
                fixture_path: str | None = None, model_fn=None,
                progress=None) -> dict:
    """Runs one weekly cycle and returns the finished run record.

    progress: optional callable(done, total) so the dashboard can show live
    progress through the slow per-conversation model pass.
    """
    start, end = chatbase_client.week_window(reference)
    week_start, week_end = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    run_id = db.start_run(week_start, week_end)
    logger.info("Weekly run %s covering %s to %s", run_id, week_start, week_end)

    try:
        # --- fetch ---
        if fixture_path:
            conversations = load_fixture(fixture_path)
            truncated = False
            logger.info("Loaded %s conversations from fixture %s", len(conversations), fixture_path)
        else:
            result = chatbase_client.fetch_conversations(start, end)
            if result["error"]:
                return db.finish_run(run_id, status=db.STATUS_FAILED, error=result["error"])
            conversations = result["conversations"]
            truncated = result["truncated"]
            logger.info("Fetched %s conversations across %s page(s)",
                        len(conversations), result["pages_fetched"])

        for conv in conversations:
            db.save_conversation(run_id, conv)

        # --- analyse ---
        analyses: dict[str, dict] = {}
        total = len(conversations)
        for i, conv in enumerate(conversations, start=1):
            cached = db.get_analysis(conv["id"])
            # Re-use a previous analysis unless it failed to parse -- re-running
            # a week should not re-pay ~30-60s of CPU per conversation.
            if cached and cached.get("parse_ok"):
                analyses[conv["id"]] = {
                    **cached,
                    "resolved": bool(cached.get("resolved")),
                    "is_lead": bool(cached.get("is_lead")),
                    "bot_failed": bool(cached.get("bot_failed")),
                }
            else:
                analysis = analyzer.analyse_conversation(conv, model_fn=model_fn)
                db.save_analysis(run_id, conv["id"], analysis)
                analyses[conv["id"]] = analysis
            if progress:
                progress(i, total)
            if i % 10 == 0:
                logger.info("Analysed %s/%s", i, total)

        # --- aggregate + render ---
        stats = analyzer.aggregate(conversations, analyses)
        model_note = f"Analysis by {settings.CHAT_MODEL} running locally."
        full_html = reporter.render_report(stats, week_start, week_end,
                                            truncated=truncated, model_note=model_note)

        # --- email ---
        email_status, email_detail = "skipped", "Email sending was not requested."
        if send_email:
            body = reporter.render_report(
                stats, week_start, week_end, redacted=settings.CHAT_REDACT_EMAIL,
                truncated=truncated, model_note=model_note)
            outcome = mailer.send(reporter.render_subject(stats, week_start, week_end), body)
            email_status = "sent" if outcome["success"] else "failed"
            email_detail = outcome["detail"]
            logger.info("Email %s: %s", email_status, email_detail)

        return db.finish_run(
            run_id, status=db.STATUS_COMPLETE, conversation_count=total,
            analysed_count=len(analyses), truncated=truncated, stats=stats,
            report_html=full_html, email_status=email_status, email_detail=email_detail)

    except Exception as e:
        logger.exception("Weekly run failed")
        return db.finish_run(run_id, status=db.STATUS_FAILED, error=str(e))


def status_lines() -> list[tuple[str, bool, str]]:
    """(label, ok, detail) for each dependency -- surfaced in the dashboard so
    it is obvious what still needs configuring."""
    model_ok, model_detail = llm.is_available()
    return [
        ("Chatbase", chatbase_client.is_configured(), chatbase_client.config_status()),
        ("Analysis model", model_ok, model_detail),
        ("Email", mailer.is_configured(), mailer.config_status()),
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly Chatbase conversation analysis.")
    parser.add_argument("--fixture", help="Analyse conversations from a JSON file instead of the API")
    parser.add_argument("--no-email", action="store_true", help="Generate the report but do not send it")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show configuration status and the target window, then exit")
    parser.add_argument("--list-agents", action="store_true",
                        help="List the agent ids this API key can see, then exit")
    args = parser.parse_args()

    if args.list_agents:
        agents = chatbase_client.list_agents()
        if not agents:
            print("No agents returned. Check CHATBASE_API_KEY in .env.")
            sys.exit(1)
        print("Agents visible to this API key:")
        for a in agents:
            marker = "  <-- configured" if a["id"] == settings.CHAT_AGENT_ID else ""
            print(f"  {a['id']}   {a['name']}{marker}")
        sys.exit(0)

    if args.dry_run:
        start, end = chatbase_client.week_window()
        print(f"Window: {start:%Y-%m-%d} to {end:%Y-%m-%d}")
        for _label, ok, detail in status_lines():
            # config_status() is already self-describing, so don't repeat the label.
            print(f"  [{'ok' if ok else '--'}] {detail}")
        sys.exit(0)

    run = run_weekly(send_email=not args.no_email, fixture_path=args.fixture)
    print()
    print(f"Run {run['id']} {run['status']}: {run['conversation_count']} conversations, "
          f"email {run['email_status']} ({run['email_detail']})")
    if run.get("error"):
        print(f"Error: {run['error']}")
        sys.exit(1)
