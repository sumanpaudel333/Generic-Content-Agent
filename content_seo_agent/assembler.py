"""
Stage 4: Assembly.

Takes the model's structured JSON output (overview, features,
applications) and assembles it into the final HTML. Everything
compliance-critical -- the regulated-product disclaimer, ratio/quantity
details, delivery copy, phone links -- is injected here by code, never
trusted from the model's own output. The model is good at tone and
structure, not at being a source of truth.
"""
import html

from config import settings


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def build_mix_line_html(is_regulated: bool, ratio: str, quantity_detail: str) -> str:
    """The regulated-mix detail line prepended to the features list, e.g.
    "Mixed to a ratio of 8% 12:1 (4x20kg component)" -- built from
    title_parser.py's deterministic extraction, never from the model."""
    if not (is_regulated and (ratio or quantity_detail)):
        return ""
    label = _esc(settings.REGULATED_RATIO_LABEL)
    mix_line = f"{label} "
    if ratio:
        mix_line += f"<b>{_esc(ratio)}</b>"
    if quantity_detail:
        mix_line += f" ({_esc(quantity_detail)})" if ratio else f"<b>{_esc(quantity_detail)}</b>"
    return mix_line


def build_fixed_tail_html(is_regulated: bool) -> str:
    """The delivery/pickup copy and (if applicable) the regulated-product
    disclaimer -- always the same for every product, sourced entirely
    from config.yaml, never from the model. Reviewers see this as a
    fixed, non-editable block since it's meant to stay centrally
    controlled rather than vary per product."""
    parts = []
    delivery = settings.delivery_html()
    if delivery:
        parts.append(delivery)
    if is_regulated and settings.REGULATED_DISCLAIMER_HTML:
        parts.append(settings.REGULATED_DISCLAIMER_HTML)
    return "\n\n".join(parts)


def build_editable_parts_html(draft_json: dict, is_regulated: bool, ratio: str, quantity_detail: str) -> str:
    """The model-controlled portion of the final HTML -- overview,
    features (with the mix-line prepended when applicable), and
    applications. This is the part a reviewer can actually edit; the
    delivery/disclaimer tail (build_fixed_tail_html) is appended
    separately and is not part of this."""
    overview = _esc(draft_json.get("overview", ""))
    features = [_esc(f) for f in draft_json.get("features", []) or []]
    applications = [_esc(a) for a in draft_json.get("applications", []) or []]

    parts = [f"<p>{overview}</p>"]

    heading_style = settings.heading_style_attr()

    mix_line = build_mix_line_html(is_regulated, ratio, quantity_detail)
    if features:
        if mix_line:
            features = [mix_line] + features
        feature_items = "\n".join(f"  <li>{f}</li>" for f in features)
        parts.append(f"<p><b{heading_style}>Features & Benefits:</b></p>\n<ul>\n{feature_items}\n</ul>")

    if applications:
        app_items = "\n".join(f"  <li>{a}</li>" for a in applications)
        parts.append(f"<p><b{heading_style}>Applications:</b></p>\n<ul>\n{app_items}\n</ul>")

    return "\n\n".join(parts)


def assemble_html(
    title: str,
    draft_json: dict,
    is_regulated: bool = False,
    ratio: str = "",
    quantity_detail: str = "",
) -> str:
    """
    Builds the final product description HTML from a structured draft.

    draft_json: {"overview": str, "features": [str, ...], "applications": [str, ...]}
    is_regulated / ratio / quantity_detail: from title_parser.py,
        NOT from the model -- these are the verified, code-extracted facts.
    """
    parts = [build_editable_parts_html(draft_json, is_regulated, ratio, quantity_detail)]
    tail = build_fixed_tail_html(is_regulated)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)
