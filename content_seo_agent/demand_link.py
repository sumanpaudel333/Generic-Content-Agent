"""
The one-way link from Chat Insights into the content agent's drafting order.

Kept as its own tiny module so the dependency is visible and reversible. The
content pipeline proper (batch_runner, pipeline, assembler) knows nothing about
chat: it accepts a {term: weight} mapping and sorts by it. Only the jobs that
kick off a batch reach across, and only through here.

Everything is wrapped: Chat Insights being absent, unconfigured, mid-migration
or simply broken must never stop product descriptions being drafted. The
failure mode is the drafting order you already had.
"""
import logging

logger = logging.getLogger("content_seo_agent.demand_link")


def demand_terms() -> dict:
    """What customers have been asking about, or {} if that cannot be read."""
    try:
        from chat_insights import demand
    except Exception:
        logger.info("Chat Insights not available; drafting order unchanged")
        return {}
    try:
        return demand.current_terms()
    except Exception:
        logger.exception("Could not read demand terms; drafting order unchanged")
        return {}
