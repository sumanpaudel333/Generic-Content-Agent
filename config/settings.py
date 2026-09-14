"""
Loads business configuration from config.yaml (gitignored -- copy from
config.example.yaml and fill in your own details). Falls back to the
example config with a warning if config.yaml doesn't exist yet, so the
project is runnable out of the box for exploration, but nothing
company-specific ever lives in the codebase itself.
"""
import os
import re
import warnings

import yaml

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.yaml")
_EXAMPLE_PATH = os.path.join(_CONFIG_DIR, "config.example.yaml")


def _load_raw_config() -> dict:
    path = _CONFIG_PATH
    if not os.path.exists(path):
        warnings.warn(
            f"config/config.yaml not found -- using config/config.example.yaml as a "
            f"fallback with generic placeholder data. Copy config.example.yaml to "
            f"config.yaml and fill in your own business details before real use.",
            stacklevel=2,
        )
        path = _EXAMPLE_PATH
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


_cfg = _load_raw_config()

# ---------------------------------------------------------------------------
# Business info
# ---------------------------------------------------------------------------
_business = _cfg.get("business", {})
BUSINESS_NAME = _business.get("name", "Your Company")
BUSINESS_DESCRIPTION = _business.get("description", "a company selling physical products")
LOCATIONS = _business.get("locations", [])
SERVICE_REGIONS = _business.get("service_regions", [])
PHONE_DISPLAY = _business.get("phone_display", "")
PHONE_TEL = _business.get("phone_tel", "")
HEADING_COLOR = _business.get("heading_color", "")

# ---------------------------------------------------------------------------
# Regulated / special-handling product category (optional)
# ---------------------------------------------------------------------------
_regulated = _cfg.get("regulated_product", {}) or {}
REGULATED_ENABLED = _regulated.get("enabled", False)
REGULATED_KEYWORD_RE = (
    re.compile(_regulated["keyword_pattern"], re.IGNORECASE)
    if REGULATED_ENABLED and _regulated.get("keyword_pattern")
    else None
)
REGULATED_DISCLAIMER_HTML = _regulated.get("disclaimer_html", "").strip()
REGULATED_RATIO_RE = (
    re.compile(_regulated["ratio_pattern"])
    if _regulated.get("ratio_pattern")
    else None
)
REGULATED_RATIO_LABEL = _regulated.get("ratio_label", "Mixed to a ratio of")
REGULATED_QUANTITY_DETAIL_RE = (
    re.compile(_regulated["quantity_detail_pattern"], re.IGNORECASE)
    if _regulated.get("quantity_detail_pattern")
    else None
)

# ---------------------------------------------------------------------------
# Standards whitelist
# ---------------------------------------------------------------------------
STANDARDS_WHITELIST = _cfg.get("standards_whitelist", {}) or {}

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
_models = _cfg.get("models", {}) or {}
SMALL_MODEL_NAME = _models.get("small_model_name", "content-agent")
OLLAMA_URL = _models.get("ollama_url", "http://127.0.0.1:11434/api/chat")
CLAUDE_MODEL = _models.get("claude_model", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Pipeline tuning
# ---------------------------------------------------------------------------
_pipeline_cfg = _cfg.get("pipeline", {}) or {}
DAILY_BATCH_SIZE = int(_pipeline_cfg.get("daily_batch_size", 10))
DAILY_RUN_DELAY_SECONDS = float(_pipeline_cfg.get("daily_run_delay_seconds", 1.0))
THIN_CONTENT_CHAR_THRESHOLD = int(_pipeline_cfg.get("thin_content_char_threshold", 300))
USE_ODOO_AS_PRODUCT_SOURCE = bool(_pipeline_cfg.get("use_odoo_as_product_source", False))

# ---------------------------------------------------------------------------
# Escalation chain
#
# The fine-tuned small model runs first. When its output is low confidence the
# work moves up the chain: a general local model, then Claude. Either hop can
# be switched off; with both off, low-confidence output still reaches the
# review queue, marked low so a human sees it -- nothing is ever dropped.
# ---------------------------------------------------------------------------
_escalation_cfg = _cfg.get("escalation", {}) or {}
ESCALATION_USE_FALLBACK_MODEL = bool(_escalation_cfg.get("use_fallback_model", True))
ESCALATION_FALLBACK_MODEL_NAME = str(_escalation_cfg.get("fallback_model_name", "") or "")
ESCALATION_USE_CLAUDE = bool(_escalation_cfg.get("use_claude", True))

# ---------------------------------------------------------------------------
# Approval flow (reviewer dashboard behavior)
# ---------------------------------------------------------------------------
_approval_cfg = _cfg.get("approval_flow", {}) or {}
APPROVAL_AUTO_PUBLISH = bool(_approval_cfg.get("auto_publish_on_approve", True))
APPROVAL_REQUIRE_REJECT_REASON = bool(_approval_cfg.get("require_reject_reason", False))
APPROVAL_PAGE_SIZE = int(_approval_cfg.get("page_size", 25))
MIN_OVERVIEW_LENGTH = int(_approval_cfg.get("min_overview_length", 15))
REGENERATE_TEMPERATURE = float(_approval_cfg.get("regenerate_temperature", 0.8))
REGENERATE_ESCALATE_FIRST = bool(_approval_cfg.get("regenerate_escalate_first", False))

# ---------------------------------------------------------------------------
# Reviewer-supplied product images
#
# Images are staged on this server between upload and publish, then written to
# the platform in the same action as the description. retain_days_after_verify
# is the only setting with teeth: it is how long a staged file is kept AFTER
# the image has been confirmed present on the product. Set it to 0 to reclaim
# as soon as verification passes; leave it a few days if you would rather have
# the original to hand when someone asks why a photo looks wrong.
# ---------------------------------------------------------------------------
_images_cfg = _cfg.get("product_images", {}) or {}
IMAGES_ENABLED = bool(_images_cfg.get("enabled", True))
IMAGES_MAX_PER_PRODUCT = max(1, int(_images_cfg.get("max_files_per_product", 8)))
IMAGES_MAX_FILE_MB = float(_images_cfg.get("max_file_mb", 8))
IMAGES_MAX_FILE_BYTES = int(IMAGES_MAX_FILE_MB * 1024 * 1024)
IMAGES_RETAIN_DAYS = max(0, int(_images_cfg.get("retain_days_after_verify", 7)))

# ---------------------------------------------------------------------------
# Dashboard
#
# Where this dashboard can be reached, used to put working links into alert
# emails. Overridden by DASHBOARD_BASE_URL in .env. Left blank, alerts still
# send -- they just carry no links.
# ---------------------------------------------------------------------------
_dashboard_cfg = _cfg.get("dashboard", {}) or {}
DASHBOARD_BASE_URL = str(_dashboard_cfg.get("base_url", "") or "")

# ---------------------------------------------------------------------------
# Site monitor -- starting values for site_monitor/store.py. Settings saved on
# the Site health page take over from these, one setting at a time.
# ---------------------------------------------------------------------------
SITE_MONITOR_CONFIG = dict(_cfg.get("site_monitor", {}) or {})

# ---------------------------------------------------------------------------
# Chat Insights (weekly Chatbase analysis)
# ---------------------------------------------------------------------------
_chat_cfg = _cfg.get("chat_insights", {}) or {}
CHAT_ENABLED = bool(_chat_cfg.get("enabled", False))
CHAT_AGENT_ID = str(_chat_cfg.get("chatbase_agent_id", "") or "")
CHAT_API_BASE = str(_chat_cfg.get("api_base", "https://www.chatbase.co/api/v1")).rstrip("/")
CHAT_LOOKBACK_DAYS = int(_chat_cfg.get("lookback_days", 7))
CHAT_PAGE_SIZE = max(1, min(100, int(_chat_cfg.get("page_size", 100))))
CHAT_MAX_PAGES = int(_chat_cfg.get("max_pages", 50))
CHAT_REQUEST_DELAY_SECONDS = float(_chat_cfg.get("request_delay_seconds", 0.2))
CHAT_MODEL = str(_chat_cfg.get("model", "llama3.2:3b"))
CHAT_TEMPERATURE = float(_chat_cfg.get("temperature", 0.2))
CHAT_MAX_ANALYSIS_TOKENS = int(_chat_cfg.get("max_analysis_tokens", 400))
CHAT_MIN_USER_MESSAGES = int(_chat_cfg.get("min_user_messages_for_analysis", 1))
CHAT_REDACT_EMAIL = bool(_chat_cfg.get("redact_email", True))
# ---------------------------------------------------------------------------
# Who receives which email
#
# One list per mailing, gathered here rather than scattered through the module
# that happens to send it -- "who gets what" is a question people ask about the
# system as a whole, and it should be answerable from one block of config.
#
# There is no shared default. An empty list means that mailing is not sent,
# which is how one gets switched off. .env overrides are resolved at send time
# in chat_insights/mailer.py, not here.
#
# The legacy flat keys (report_recipients / leads_recipients / alert_recipients
# under chat_insights) are still read, so an older config.yaml keeps working.
# ---------------------------------------------------------------------------
_email_cfg = _cfg.get("email", {}) or {}
_recipients_cfg = _email_cfg.get("recipients", {}) or {}


def _recipients_for(key: str, *legacy_keys: str) -> list:
    configured = _recipients_cfg.get(key)
    if configured:
        return list(configured)
    for legacy in legacy_keys:
        old = _chat_cfg.get(legacy)
        if old:
            return list(old)
    return []


MAIL_RECIPIENTS = {
    # key                what it is                                   legacy key
    "weekly_report": _recipients_for("weekly_report", "report_recipients"),
    "daily_leads": _recipients_for("daily_leads", "leads_recipients"),
    "lead_alerts": _recipients_for("lead_alerts", "alert_recipients"),
    "job_failures": _recipients_for("job_failures", "alert_recipients"),
}

# Kept as names for the two the rest of the code already referred to.
CHAT_REPORT_RECIPIENTS = MAIL_RECIPIENTS["weekly_report"]
# SMTP defaults. Every one of these is overridden by the matching .env key
# (SMTP_HOST / SMTP_PORT / SMTP_SECURITY / SMTP_FROM) -- see chat_insights.mailer.
CHAT_SMTP_HOST = str(_chat_cfg.get("smtp_host", "") or "")
CHAT_SMTP_PORT = int(_chat_cfg.get("smtp_port", 587))
CHAT_SMTP_SECURITY = str(_chat_cfg.get("smtp_security", "") or "")
CHAT_SMTP_FROM = str(_chat_cfg.get("smtp_from", "") or "")
CHAT_SMTP_FROM_NAME = str(_chat_cfg.get("smtp_from_name", "") or "")

# The display name on the emails about ACCOUNTS -- invitations and password
# resets. Separate from the one above on purpose: a message asking somebody to
# set a password must not arrive under the name of the weekly chat report,
# which is both confusing and the exact shape of an email people are told to
# distrust. Blank falls back to the dashboard's own name.
ACCOUNT_EMAIL_FROM_NAME = str(_chat_cfg.get("account_email_from_name", "") or "")
CHAT_SMTP_TLS_CA_FILE = str(_chat_cfg.get("smtp_tls_ca_file", "") or "")
CHAT_SMTP_TLS_VERIFY = bool(_chat_cfg.get("smtp_tls_verify", True))

# Lead alerts. Every detected lead is emailed as it is found, because the CRM
# hand-off (a Chatbase action calling Zapier) sometimes does not fire and a
# lead that only exists in a weekly report is a lead that waits days.
CHAT_ALERT_LEADS = bool(_chat_cfg.get("alert_on_leads", True))
# Phone numbers and email addresses that belong to US, not to a customer. The
# bot quotes the branch numbers in nearly every conversation, so without this
# list every chat looks like a lead offering us our own phone number back.
# Branch numbers cannot be derived from anything else in config -- list them.
CHAT_OWN_CONTACTS = list(_chat_cfg.get("own_contacts", []) or [])

# Daily lead digest. Separate from the weekly report: a customer told their
# details had been forwarded, when they were not, cannot wait for Thursday.
# Recipients default to the weekly report's list when not set separately --
# leads usually go to sales rather than to whoever reads the analysis.
# Model-assisted lead extraction. The deterministic reader finds phone numbers
# and emails reliably; names and the enquiry need to know what words mean
# ("paul strachan" and "garden stakes" are the same shape). Runs over leads
# only -- a handful a day -- and every value it returns is checked back against
# the customer's own words before it is used, so it can add detail but cannot
# invent a contact.
_extract_cfg = _chat_cfg.get("lead_extraction", {}) or {}
CHAT_LEAD_EXTRACT_ENABLED = bool(_extract_cfg.get("enabled", False))
CHAT_LEAD_EXTRACT_MODEL = str(_extract_cfg.get("model", "claude") or "claude")
CHAT_LEAD_EXTRACT_LOCAL_MODEL = str(_extract_cfg.get("local_model", "") or "")

CHAT_LEADS_LOOKBACK_DAYS = max(1, int(_chat_cfg.get("leads_lookback_days", 3)))
CHAT_LEADS_RECIPIENTS = MAIL_RECIPIENTS["daily_leads"]
CHAT_LEADS_EMAIL_WHEN_EMPTY = bool(_chat_cfg.get("leads_email_when_empty", False))
# Which lead types are worth an email. Both are, by default: every lead on the
# list has contact details, or it would not be on the list.
CHAT_ALERT_LEAD_TYPES = list(_chat_cfg.get(
    "alert_lead_types", ["form_submission", "contact_shared"]) or [])
CHAT_ALERT_JOB_FAILURES = bool(_chat_cfg.get("alert_on_job_failure", True))

# ---------------------------------------------------------------------------
# Derived: system prompts
#
# IMPORTANT: if you fine-tune a small model against these prompts (see
# the training pipeline in the README), the model learns the EXACT
# wording produced here. Changing business.name or business.description
# later will drift the live prompt away from what the model was trained
# on -- for a deployed fine-tuned model, keep these values stable, or
# re-run fine-tuning after a change.
# ---------------------------------------------------------------------------

SYSTEM_CLASSIFY = (
    f"You are a content classification assistant for {BUSINESS_NAME}, {BUSINESS_DESCRIPTION}. "
    "Given a product title and its current description (which may be empty), output ONLY a "
    "JSON object with these fields: "
    '{"is_regulated": true/false, "content_status": "missing"|"thin"|"plain_text_needs_formatting"'
    '|"regulated_missing_disclaimer"|"good", "product_type": "bulk_bag"|"roll"|"bagged"'
    '|"each_or_pack"|"other"}. '
    "is_regulated is true only if the product itself belongs to the regulated/special-handling "
    "category as a defining attribute of the product name, not merely if a related word appears "
    "anywhere in unrelated text."
)

SYSTEM_DRAFT = (
    f"You are a product content writer for {BUSINESS_NAME}, {BUSINESS_DESCRIPTION}. "
    "Given a product title, write structured product content as a JSON object with fields: "
    '{"overview": "1-2 sentence description", "features": ["feature 1", "feature 2", ...], '
    '"applications": ["application 1", "application 2", ...]}. '
    "House style: no em dashes, no exaggerated marketing language, mention concrete "
    "specifications (sizes, materials, quantities) where relevant. Do not include delivery "
    "information, disclaimers, or pricing. Do not state or imply compliance with any named "
    "standard, certification, or regulation unless it is explicitly given to you in the input. "
    "Output ONLY the JSON object."
)


def heading_style_attr() -> str:
    """style="color:..." attribute for section headings (Features & Benefits,
    Applications, Delivery & Pickup) in generated copy, or "" if no brand
    color is configured -- shared so every heading across the assembled
    HTML stays visually consistent from one config value."""
    return f' style="color:{HEADING_COLOR}"' if HEADING_COLOR else ""


def delivery_html() -> str:
    if not LOCATIONS:
        return ""
    phone_line = ""
    if PHONE_DISPLAY and PHONE_TEL:
        phone_line = f' For urgent orders, call <a href="tel:{PHONE_TEL}">{PHONE_DISPLAY}</a>.'
    locations_str = " and ".join(LOCATIONS) if len(LOCATIONS) <= 2 else ", ".join(LOCATIONS)
    regions_str = ", ".join(
        f"<b>{r}</b>" if i == 0 else r for i, r in enumerate(SERVICE_REGIONS)
    ) if SERVICE_REGIONS else ""
    regions_clause = f", including {regions_str}, and beyond" if regions_str else ""
    return (
        f"<p><b{heading_style_attr()}>Delivery & Pickup:</b><br>\n"
        f"With locations at {locations_str}, {BUSINESS_NAME} supplies products throughout "
        f"the surrounding area{regions_clause}. Choose to <b>collect in-store "
        f"or arrange delivery</b> directly to your site with flexible scheduling at "
        f"checkout.{phone_line}</p>"
    )
