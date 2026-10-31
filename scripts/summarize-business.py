import json
from pathlib import Path

for kind in ("products", "tickets", "reviews"):
    data = json.loads(Path(f".data/discovery-{kind}.json").read_text(encoding="utf-8"))
    print("\nKIND", kind)
    for item in data["responses"]:
        url = item["url"].split("?")[0]
        if (
            (kind == "products" and "/mall/goodsList" not in url)
            or (kind == "tickets" and not any(x in url.lower() for x in ("workorder", "work_order")))
            or (kind == "reviews" and not url.endswith("/reviews/list"))
        ):
            continue
        result = item["data"].get("result")
        print("URL", url, "REQUEST", item["request"].get("postData", "")[:2000])
        if isinstance(result, dict):
            print(
                "FIELDS",
                json.dumps(
                    {
                        k: (f"list[{len(v)}]" if isinstance(v, list) else (list(v) if isinstance(v, dict) else v))
                        for k, v in result.items()
                    },
                    ensure_ascii=False,
                ),
            )
            for k, v in result.items():
                if isinstance(v, list) and v:
                    print("FIRST", k, json.dumps(v[0], ensure_ascii=False)[:7000])
        else:
            print("RESULT", str(result)[:400])
    print("TABLE_HEADERS", [t["text"][:700] for t in data["dom"]["tables"][:1]])
