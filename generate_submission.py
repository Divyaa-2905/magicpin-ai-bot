"""
Generate submission.jsonl from expanded/test_pairs.json
"""

import json
from pathlib import Path
from bot import compose


def main():
    root = Path(__file__).parent
    expanded_dir = root / "expanded"

    test_pairs_path = expanded_dir / "test_pairs.json"
    if not test_pairs_path.exists():
        print("test_pairs.json not found in expanded/")
        return

    with open(test_pairs_path, "r", encoding="utf-8") as f:
        pairs = json.load(f)["pairs"]

    # Preload categories
    categories = {}
    for cat_file in (expanded_dir / "categories").glob("*.json"):
        with open(cat_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            categories[data.get("slug", cat_file.stem)] = data

    submission_path = root / "submission.jsonl"
    count = 0

    with open(submission_path, "w", encoding="utf-8") as out_f:
        for pair in pairs:
            test_id = pair["test_id"]
            trg_id = pair["trigger_id"]
            m_id = pair["merchant_id"]
            c_id = pair.get("customer_id")

            # Load trigger
            with open(expanded_dir / "triggers" / f"{trg_id}.json", "r", encoding="utf-8") as f:
                trigger = json.load(f)

            # Load merchant
            with open(expanded_dir / "merchants" / f"{m_id}.json", "r", encoding="utf-8") as f:
                merchant = json.load(f)

            # Load customer if applicable
            customer = None
            if c_id:
                cust_path = expanded_dir / "customers" / f"{c_id}.json"
                if cust_path.exists():
                    with open(cust_path, "r", encoding="utf-8") as f:
                        customer = json.load(f)

            # Load category
            cat_slug = merchant.get("category_slug", "generic")
            category = categories.get(cat_slug, {"slug": cat_slug, "voice": {}, "digest": [], "offer_catalog": []})

            # Compose
            res = compose(category, merchant, trigger, customer)

            line_obj = {
                "test_id": test_id,
                "body": res["body"],
                "cta": res["cta"],
                "send_as": res["send_as"],
                "suppression_key": res["suppression_key"],
                "rationale": res["rationale"]
            }

            out_f.write(json.dumps(line_obj, ensure_ascii=False) + "\n")
            count += 1

    print(f"Successfully generated {count} test pairs in {submission_path}")


if __name__ == "__main__":
    main()
