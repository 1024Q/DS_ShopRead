import json, sys
from pathlib import Path

for kind in sys.argv[1:]:
    p = Path(f".data/discovery-{kind}.json")
    if not p.exists():
        continue
    data = json.loads(p.read_text(encoding="utf-8"))
    print("\nKIND", kind)
    for item in data["responses"]:
        url = item["url"].split("?")[0]
        if not any(
            x in url
            for x in (
                "/new/pack/center/list",
                "/invoice_list",
                "/afterSales/queryList",
                "/afterSales/queryParams",
                "/proof/list",
                "/queryInterceptOrderList",
                "/queryGoodsNavigatorList",
                "/queryGoodsInfo",
                "/queryMallInfo",
                "/queryMallAlarm",
                "/blacktag/goods/",
            )
        ):
            continue
        raw = item["data"]
        result = raw.get("result", raw)
        print("URL", url, "BODY", item["request"].get("postData", "")[:1000])

        def shape(value, depth=0):
            if isinstance(value, dict):
                return {k: shape(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return ["length:" + str(len(value)), shape(value[0], depth + 1) if value else None]
            return value if depth < 2 else type(value).__name__

        print("RESULT", json.dumps(shape(result), ensure_ascii=False)[:4500])
