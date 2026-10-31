"""Read the merchant homepage's verified initial statistics, with a compact DOM fallback.

Observed on 2026-10-28: __NEXT_DATA__.props.pageProps.coreData contains the
homepage response. The rendered cards use realTimeManagementData.rtDataOverView
(amount in cents; promotion spend in thousandths of yuan) and bottomnums.
We return only these fields, never the surrounding account/permission data.
"""

import asyncio
import json
import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from .read_errors import PageReadError

LABELS = {
    "sales": "成交金额",
    "orders": "成交订单数",
    "ad_spend": "推广花费",
    "visitors": "商品访客数",
    "views": "商品浏览量",
    "reviews_today": "商品评价数",
    "pending_payment": "待付款",
    "pending_shipping": "待发货",
    "pending_aftersales": "退款/售后",
    "pending_tickets": "待处理工单",
    "shipping_due": "即将逾期发货",
    "aftersales_due": "售后过期预警",
    "inquiry_conversion": "询单转化率",
}
REALTIME = {
    "sales": ("curPayOrdrAmt", "payOrdrAmtShow", 100),
    "orders": ("curPayOrdrCnt", "payOrdrCntShow", 1),
    "ad_spend": ("promotionSpend", "promotionSpendShow", 1000),
    "visitors": ("guvOned", "guvOnedShow", 1),
    "views": ("gpvOned", "gpvOnedShow", 1),
    "reviews_today": ("goodsReviewCnt", "goodsReviewCntShow", 1),
}
BOTTOM = {
    "pending_payment": ("waitPayNum",),
    "pending_shipping": ("waitShipNum",),
    "pending_aftersales": ("aftersalesNew", "totalWaitHandleCount"),
    "pending_tickets": ("pendingOrder",),
    "shipping_due": ("unShip12H",),
    "aftersales_due": ("aftersalesNew", "expireEarlyWarningCount"),
}

SNAPSHOT_SCRIPT = (
    r"""(()=>{
  const valid=location.hostname==='mms.pinduoduo.com'&&/^\/home\/?$/.test(location.pathname);
  if(!valid)return {route_valid:false};
  const labels=LABELS, pick=(obj,keys)=>Object.fromEntries(keys.filter(k=>Object.prototype.hasOwnProperty.call(obj||{},k)).map(k=>[k,obj[k]]));
  let core={};
  try {
    const root=JSON.parse(document.getElementById('__NEXT_DATA__')?.textContent||'null')?.props?.pageProps?.coreData;
    if(root&&root.data){
      const rt=root.data.realTimeManagementData||{},bottom=root.data.bottomnums||{};
      core={status:pick(root.status,['realTimeManagementData','bottomnums']),showPromotion:rt.showPromotion,
        realtime:pick(rt.rtDataOverView,REALTIME_FIELDS),
        bottom:{...pick(bottom,['waitPayNum','waitShipNum','pendingOrder','unShip12H','inqueryRate']),
          aftersalesNew:pick(bottom.aftersalesNew,['totalWaitHandleCount','expireEarlyWarningCount'])}};
    }
  } catch (_) {}
  const finite=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0,rtFields=REALTIME_SPEC,bottomFields=BOTTOM_SPEC;
  const readable=key=>{
    if(rtFields[key]){const [field,flag]=rtFields[key];return core.status?.realTimeManagementData===1&&finite(core.realtime?.[field])&&core.realtime?.[flag]===0&&(key!=='ad_spend'||core.showPromotion===true);}
    if(core.status?.bottomnums!==1)return false;
    if(key==='inquiry_conversion')return finite(core.bottom?.inqueryRate);
    return finite((bottomFields[key]||[]).reduce((v,k)=>v?.[k],core.bottom));
  };
  const missing=Object.keys(labels).filter(key=>!readable(key)),dom={};
  // The normal complete SSR response needs no card/tree scan or layout read.
  const leaves=missing.length?[...document.querySelectorAll('span,p,div,a')].filter(e=>e.children.length===0):[];
  const escape=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  for(const key of missing){const label=labels[key];
    const element=leaves.find(e=>e.textContent.trim()===label);let node=element;
    for(let depth=0;node&&depth<4;depth++,node=node.parentElement){
      const match=(node.innerText||'').match(new RegExp(escape(label)+'\\s*\\n\\s*([\\d,]+(?:\\.\\d+)?)'));
      if(match){dom[key]=match[1];break;}
    }
  }
  const clock=/实时数据更新时间\s*[:：]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/;
  let source_time=(document.querySelector('[class*="ManageHeader_header__"]')?.textContent.match(clock)||[])[1]||null;
  if(!source_time){
    const label=[...document.querySelectorAll('span,p')].find(e=>e.textContent.includes('实时数据更新时间'));
    source_time=(label?.textContent.match(clock)||label?.parentElement?.textContent.match(clock)||[])[1]||null;
  }
  return {route_valid:true,origin:performance.timeOrigin,core,dom,source_time};
})()""".replace(
        "REALTIME_FIELDS", json.dumps([value for field, flag, _ in REALTIME.values() for value in (field, flag)])
    )
    .replace("REALTIME_SPEC", json.dumps(REALTIME))
    .replace("BOTTOM_SPEC", json.dumps(BOTTOM))
    .replace("LABELS", json.dumps(LABELS, ensure_ascii=False))
)


def number(value):
    # Native cards reject negative/non-numeric/missing values instead of showing zero.
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def display_round(value):
    # Match the page's Number.toFixed(2), including binary-float halfway cases.
    try:
        return float(Decimal.from_float(value).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
    except InvalidOperation, ValueError:
        return None


def structured(core):
    values = {key: None for key in LABELS}
    if not isinstance(core, dict):
        return values
    status = core.get("status") or {}
    if not isinstance(status, dict):
        return values
    rt = core.get("realtime") or {}
    if status.get("realTimeManagementData") == 1 and isinstance(rt, dict):
        for key, (field, flag, divisor) in REALTIME.items():
            value = number(rt.get(field))
            if value is None or rt.get(flag) != 0:
                continue
            if key == "ad_spend" and core.get("showPromotion") is not True:
                continue
            values[key] = display_round(value / divisor) if divisor != 1 else value
    bottom = core.get("bottom") or {}
    if status.get("bottomnums") == 1 and isinstance(bottom, dict):
        for key, path in BOTTOM.items():
            value = bottom
            for part in path:
                value = value.get(part) if isinstance(value, dict) else None
            values[key] = number(value)
        rate = number(bottom.get("inqueryRate"))
        if rate is not None:
            values["inquiry_conversion"] = display_round(rate * 100)
    return values


def metrics(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("route_valid") is not True:
        raise PageReadError("经营概况页面已离开店铺首页，请重新读取")
    values = structured(snapshot.get("core"))
    fallback = snapshot.get("dom") or {}
    for key in LABELS:
        if values[key] is None and isinstance(fallback, dict):
            try:
                values[key] = number(float(str(fallback.get(key, "")).replace(",", "")))
            except TypeError, ValueError, OverflowError:
                pass
    if values["sales"] is None or values["orders"] is None:
        raise ValueError("未读取到经营数据，可能需要重新登录或页面尚未加载")
    values["source_time"] = snapshot.get("source_time") or None
    return values


async def collect(page):
    # Each open('overview') navigates/reloads first; never retain __NEXT_DATA__
    # between batches. Only unsupported/late page fields use this bounded retry.
    for _ in range(25):
        snapshot = await page.evaluate(SNAPSHOT_SCRIPT)
        if not isinstance(snapshot, dict):
            raise PageReadError("经营概况页面响应无效，请重新读取")
        expected = getattr(page, "overview_origin", None)
        if expected is not None and snapshot.get("origin") != expected:
            raise PageReadError("经营概况文档已切换，保留上次数据后重新读取")
        try:
            return {"metrics": metrics(snapshot), "rows": [], "complete": True}
        except PageReadError:
            raise
        except ValueError:
            await asyncio.sleep(0.4)
    raise ValueError("经营指标未加载完成，请检查店铺是否有数据查看权限")
