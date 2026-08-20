"""
Builds fine-tuning training data (classification + drafting tasks) from
your own product catalog export.

Usage (from project root):
    python training/prepare_training_data.py --input your_export.xlsx --output-dir training_data

Deliberately reuses the SAME deterministic logic the live pipeline uses
(content_seo_agent.title_parser, content_seo_agent.content_status) --
both driven by your config.yaml -- so there's no drift between what the
model gets trained to recognize and what the pipeline checks at
runtime. The system prompts baked into every training example
(settings.SYSTEM_CLASSIFY / settings.SYSTEM_DRAFT) are also built from
your config, so this script needs no company-specific edits at all --
everything comes from config.yaml.

Output (in --output-dir):
    classify_train.jsonl / classify_val.jsonl
    draft_train.jsonl / draft_val.jsonl
    combined_train.jsonl / combined_val.jsonl  (both tasks merged --
        recommended for fine-tuning a single model that handles both)

Requires your export to have product_id/product_title/product_description
columns (same shape the batch runner expects -- headerless files are
auto-detected the same way).
"""
import argparse
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from config import settings
from content_seo_agent.batch_runner import load_products
from content_seo_agent.title_parser import parse_title
from content_seo_agent.content_status import determine_content_status


def extract_fields(html: str) -> dict:
    """
    Parses an existing HTML description into overview/features/applications,
    used to build drafting-task training targets from your catalog's
    already-good descriptions. Assumes a structure with <p> overview
    text and <b>Features & Benefits</b> / <b>Applications</b> headed
    <ul> lists -- the same structure this project's own assembler.py
    produces, so descriptions the pipeline has already generated and
    you've approved are automatically good training data for next time.

    If your existing catalog uses different section headers, adjust
    the header-matching keywords below.
    """
    if not html or not html.strip():
        return {"overview": "", "features": [], "applications": []}

    soup = BeautifulSoup(html, "lxml")
    overview_parts, features, applications = [], [], []
    current_section = "overview"

    for el in soup.find_all(["p", "ul"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        header_b = el.find("b")
        header_text = header_b.get_text(strip=True).lower() if header_b else ""

        if "features" in header_text or "benefit" in header_text:
            current_section = "features"
            continue
        elif "application" in header_text:
            current_section = "applications"
            continue
        elif "delivery" in header_text or "pickup" in header_text:
            current_section = "delivery"
        elif "important information" in header_text:
            current_section = "disclaimer"

        if el.name == "ul":
            items = [li.get_text(" ", strip=True) for li in el.find_all("li")]
            if current_section == "features":
                features.extend(items)
            elif current_section == "applications":
                applications.extend(items)
        else:
            if current_section == "overview":
                overview_parts.append(text)

    return {"overview": " ".join(overview_parts), "features": features, "applications": applications}


def guess_product_type(title: str) -> str:
    """
    Simple heuristic classification of packaging/unit type from the
    title. Adjust the keyword matches below to fit your own catalog's
    conventions if these categories don't fit your product range.
    """
    t = (title or "").lower()
    if "bulk bag" in t or "craned" in t:
        return "bulk_bag"
    if re.search(r"\broll\b", t):
        return "roll"
    if re.search(r"\d+\s*kg\b", t) or "bag" in t:
        return "bagged"
    if re.search(r"\beach\b|\bpack\b", t):
        return "each_or_pack"
    return "other"


def build_classify_examples(df) -> list[dict]:
    examples = []
    for _, row in df.iterrows():
        title = str(row["product_title"])
        desc = str(row["product_description"] or "")
        title_facts = parse_title(title)
        content_status = determine_content_status(title, desc)
        product_type = guess_product_type(title)

        desc_snippet = desc[:400] if desc else "(no description)"
        user_msg = f"Product title: {title}\nCurrent description: {desc_snippet}"
        assistant_json = json.dumps({
            "is_regulated": title_facts["is_regulated"],
            "content_status": content_status,
            "product_type": product_type,
        })
        examples.append({
            "messages": [
                {"role": "system", "content": settings.SYSTEM_CLASSIFY},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_json},
            ]
        })
    return examples


def build_draft_examples(df) -> list[dict]:
    examples = []
    for _, row in df.iterrows():
        title = str(row["product_title"])
        desc = str(row["product_description"] or "")
        content_status = determine_content_status(title, desc)
        if content_status != "good":
            continue  # only learn from your catalog's already-good descriptions

        parsed = extract_fields(desc)
        if len(parsed["overview"]) < 20 or not parsed["features"]:
            continue  # too sparse to be a useful training example

        user_msg = f"Product title: {title}"
        assistant_json = json.dumps({
            "overview": parsed["overview"],
            "features": parsed["features"],
            "applications": parsed["applications"],
        })
        examples.append({
            "messages": [
                {"role": "system", "content": settings.SYSTEM_DRAFT},
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": assistant_json},
            ]
        })
    return examples


def split(examples: list, val_frac: float = 0.1) -> tuple[list, list]:
    n_val = max(1, int(len(examples) * val_frac))
    return examples[n_val:], examples[:n_val]


def write_jsonl(path: str, examples: list):
    with open(path, "w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Build fine-tuning training data from a product export.")
    parser.add_argument("--input", required=True, help="Path to product export (.xlsx or .csv)")
    parser.add_argument("--output-dir", default="training_data", help="Where to write the JSONL files")
    parser.add_argument("--val-fraction", type=float, default=0.1, help="Fraction held out for validation")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Loading products from {args.input} ...")
    df = load_products(args.input)
    print(f"Loaded {len(df)} products.")

    print("Building classification examples...")
    classify_examples = build_classify_examples(df)
    print(f"  {len(classify_examples)} classification examples.")

    print("Building drafting examples (from already-good descriptions only)...")
    draft_examples = build_draft_examples(df)
    print(f"  {len(draft_examples)} drafting examples.")

    if len(draft_examples) < 200:
        print(f"  NOTE: only {len(draft_examples)} drafting examples found. Fine-tuning "
              f"typically wants at least a few hundred for the model to reliably pick up "
              f"style. If your catalog doesn't have many complete existing descriptions "
              f"yet, consider writing/approving a batch by hand first, or drafting with the "
              f"larger model via this pipeline and using the approved output as training data.")

    print("  NOTE: drafting examples are extracted verbatim from your existing 'good' "
          "descriptions. If those descriptions have style inconsistencies (em dashes, "
          "inconsistent tone, etc.) relative to the house style you want, the model will "
          "learn those inconsistencies too. Worth a quick review pass over the output "
          "JSONL before training if style consistency matters.")

    random.seed(args.seed)
    random.shuffle(classify_examples)
    random.shuffle(draft_examples)

    classify_train, classify_val = split(classify_examples, args.val_fraction)
    draft_train, draft_val = split(draft_examples, args.val_fraction)

    combined_train = classify_train + draft_train
    combined_val = classify_val + draft_val
    random.shuffle(combined_train)
    random.shuffle(combined_val)

    os.makedirs(args.output_dir, exist_ok=True)
    write_jsonl(os.path.join(args.output_dir, "classify_train.jsonl"), classify_train)
    write_jsonl(os.path.join(args.output_dir, "classify_val.jsonl"), classify_val)
    write_jsonl(os.path.join(args.output_dir, "draft_train.jsonl"), draft_train)
    write_jsonl(os.path.join(args.output_dir, "draft_val.jsonl"), draft_val)
    write_jsonl(os.path.join(args.output_dir, "combined_train.jsonl"), combined_train)
    write_jsonl(os.path.join(args.output_dir, "combined_val.jsonl"), combined_val)

    print()
    print(f"Wrote training data to {args.output_dir}/")
    print(f"  classify: {len(classify_train)} train / {len(classify_val)} val")
    print(f"  draft:    {len(draft_train)} train / {len(draft_val)} val")
    print(f"  combined: {len(combined_train)} train / {len(combined_val)} val")
    print()
    print("Next: upload combined_train.jsonl and combined_val.jsonl to the fine-tuning "
          "notebook (training/fine_tune_model.ipynb).")


if __name__ == "__main__":
    main()
