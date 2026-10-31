// Which collector scripts implement each sub-step, and how they work.
// Paths are relative to the project root on the collector machine. Texts are the
// Simplified Chinese source strings; the UI translates them with t().

export const PIPELINE = [
  [
    'backend/scheduler.py',
    '按频率组扫描各店铺到期子步骤，生成采集需求；生成前检查队列，同一子步骤仍在排队或执行时不再重复生成',
  ],
  ['backend/collection_demands.py', '合并范围相同或可覆盖的需求，避免重复读取'],
  [
    'backend/collection_queue.py',
    '按节点总并发、单店并发、同时采集店铺数和同店同模块互斥挑选可执行任务；排队序号与预计等待由 backend/queue_eta.py 按同样规则估算',
  ],
  [
    'backend/business.py',
    'slot() 占用名额并按需唤醒店铺浏览器（默认后台静默，调试模式才显示窗口），sync() 执行读取、保存断点并写入本地库',
  ],
  ['backend/incremental_collection.py', '能增量时只补读上次快照之后的变化，否则全量读取'],
  [
    'backend/business_reader.py',
    'collect() 按模块分派到具体适配脚本；ReadPage 在店铺专用浏览器的独立工作标签中通过 CDP 读取页面与接口响应',
  ],
  ['backend/center/agent.py', '上传代理把入库结果回传中心数据库，首页与后台从中心读取'],
];

const PDD = {
  overview: {
    files: ['backend/overview_summary.py'],
    api: 'mms.pinduoduo.com/home/ · __NEXT_DATA__.props.pageProps.coreData',
    principle:
      '读取商家后台首页服务端渲染的初始数据 coreData（实时概览与底部待办数字），读不到时从页面卡片元素兜底解析。',
  },
  transactions: {
    files: [
      'backend/transactions.py',
      'backend/trade_font.py',
      'backend/trade_glyphs.js',
      'backend/summary_publication.py',
    ],
    api: '交易数据页原生时间范围控件触发的接口响应',
    principle:
      '点击交易页自带的时间范围控件，只拦截页面自身发出的接口响应；金额使用平台加密数字字体，通过字形映射还原。首页自动任务只读概况与趋势摘要，明细按需补读。',
  },
  ad_reports: {
    files: ['backend/promotions.py', 'backend/summary_publication.py'],
    api: '/mms-gateway/poseidon/api/report/queryRangeReport · queryHourlyRangeReport',
    principle: '在推广报表页使用原生日期控件触发查询并读取汇总响应；首页自动任务只保存汇总指标，不翻商品推广明细页。',
  },
  ad_products: {
    files: ['backend/promotions.py'],
    api: '/mms-gateway/venus/api/goods/promotion/v3/list',
    principle: '在商品推广列表页按页面自带分页读取各推广商品的消耗、成交与投产数据。',
  },
  goodsdata: {
    files: ['backend/goods_data.py'],
    api: '/sydney/api/goodsDataShow/',
    principle: '在商品数据页使用页面自带的周期筛选与分页读取商品维度经营数据。',
  },
  products: {
    files: ['backend/products.py', 'backend/product_images.py', 'backend/new_products.py'],
    api: 'mms.pinduoduo.com/goods/goods_list',
    principle: '通过可续传的分页器读取全部商品列表并保存断点；上新统计按商品首次创建日期（北京时间）汇总。',
  },
  orders: {
    files: ['backend/orders.py', 'backend/order_plan.py', 'backend/list_completion.py'],
    api: '/mangkhut/mms/recentOrderList',
    principle:
      '在订单查询页按状态与日期范围调用页面同源的订单列表接口分页读取；今昨订单按天划分范围，未完结订单按待付款、待发货、待收货回查，退款指标按售后单实际退款金额统计。',
  },
  aftersales: {
    files: ['backend/operations.py', 'backend/aftersales_warning.py'],
    api: '/mercury/mms/afterSales/queryList',
    principle: '在售后工作台按状态（待商家处理、待处理即将逾期等）分页读取售后单。',
  },
  tickets: {
    files: ['backend/business_reader.py'],
    api: 'mms.pinduoduo.com/aftersales/work_order/list',
    principle: '在工单列表页通过通用列表读取器按状态分页读取工单。',
  },
  reviews: {
    files: ['backend/business_reader.py'],
    api: 'mms.pinduoduo.com/goods/evaluation/index',
    principle: '在评价管理页通过通用列表读取器按星级与回复状态分页读取评价。',
  },
  packages: {
    files: ['backend/operations.py', 'backend/incremental_collection.py'],
    api: '/express_base/new/pack/center/list/get',
    principle: '在包裹中心分页读取包裹与物流异常；支持只补读上次快照之后的变化。',
  },
  invoices: {
    files: ['backend/operations.py', 'backend/invoice_fields.py'],
    api: '/omaisms/invoice/invoice_list',
    principle: '在订单开票页分页读取开票申请；明细补采只针对待开票记录补读申请字段，不推断缺失值。',
  },
  evidence: {
    files: ['backend/operations.py'],
    api: '/mercury/merchant/proof/list',
    principle: '在商家举证页分页读取待举证与已提交的举证记录。',
  },
  intercepts: {
    files: ['backend/operations.py'],
    api: '/cambridge/api/express/intercept/queryInterceptOrderList',
    principle: '在拦截快递页分页读取拦截记录，重点关注拦截失败。',
  },
  navigator: {
    files: ['backend/operations.py'],
    api: '/sydney/api/goodsNavigator/queryGoodsNavigatorList',
    principle: '在商品领航员页分页读取商品诊断结果。',
  },
  quality: {
    files: ['backend/operations.py'],
    api: '/genji/warlock/mallQualityAnalysis/queryGoodsInfo',
    principle: '在品质诊断页读取店铺与商品品质分析数据。',
  },
  brands: {
    files: ['backend/operations.py'],
    api: '/link/api/blacktag/goods/compare/price/goods/list',
    principle: '在品牌黑标权益页读取比价商品列表；需要账号具备该权限。',
  },
  activities: {
    files: ['backend/activities.py'],
    api: '/lakemms/activityGoods/statusType',
    principle: '在活动报名记录页读取近期报名记录与审核状态，不提交任何报名操作。',
  },
  ad_logs: {
    files: ['backend/promotion_logs.py'],
    api: '/mms-gateway/venus/api/log/queryOperationLogWithCount',
    principle: '使用推广平台官方操作记录查询读取推广计划的调整历史。',
  },
  tax: {
    files: ['backend/tax_info.py', 'backend/platform_tax.py', 'backend/xiaohongshu_tax.py'],
    api: '/earth/api/mallInfo/queryFinalCredentialNew',
    principle:
      '读取店铺主体资质并按四类月度报税口径计算；天猫与京东使用 platform_tax.py，小红书使用 xiaohongshu_tax.py。',
  },
  reconciliation_day: {
    files: ['backend/reconciliation.py'],
    api: 'mms.pinduoduo.com/cashier/finance/payment-bills',
    principle: '读取收银台已发布的货款日汇总，并按需展开分类明细。',
  },
  reconciliation_month: {
    files: ['backend/reconciliation.py'],
    api: 'mms.pinduoduo.com/cashier/finance/payment-bills',
    principle: '读取收银台已发布的货款月汇总，并按需展开分类明细。',
  },
};

const TAOBAO = {
  files: ['backend/taobao.py', 'backend/taobao_page.py'],
  api: '生意参谋 / 千牛 / 阿里妈妈官方页面',
  principle: '淘宝适配器：在已登录的官方页面操作原生控件，只读取页面自身产生的白名单接口响应，不重放接口。',
};
const TAOBAO_EXTRA = {
  ad_products: ['backend/taobao_ads.py'],
  ad_reports: ['backend/taobao_ads.py'],
  goodsdata: ['backend/taobao_flow.py'],
  packages: ['backend/taobao_operations.py'],
  invoices: ['backend/taobao_operations.py'],
  tax: ['backend/platform_tax.py'],
};

const STEP_NOTES = {
  attention: '重点状态：独立保存紧急范围的完整快照（backend/attention.py），不覆盖完整列表。',
  yesterday: '历史范围：切换到昨日数据读取并保存为独立快照。',
  week: '历史范围：切换到近 7 天数据读取并保存为独立快照。',
  month: '历史范围：切换到近 30 天数据读取并保存为独立快照。',
  details: '明细补采：只对待开票记录补读申请明细字段。',
  recheck: '精准回查：对指定订单逐个恢复和核对状态（backend/order_plan.py）。',
};

/** Script information for a sub-step id such as "orders@today" on a platform. */
export function stepScripts(stepId, platform = 'pdd') {
  const [kind, category] = String(stepId).split('@');
  const base = PDD[kind];
  if (!base) return null;
  const note = STEP_NOTES[category];
  if (platform === 'taobao') {
    return {
      files: [
        ...new Set([
          ...TAOBAO.files,
          ...(TAOBAO_EXTRA[kind] || []),
          ...(category === 'attention' ? ['backend/attention.py'] : []),
        ]),
      ],
      api: TAOBAO.api,
      principle: TAOBAO.principle,
      note,
    };
  }
  return {
    files: [...new Set([...base.files, ...(category === 'attention' ? ['backend/attention.py'] : [])])],
    api: base.api,
    principle: base.principle,
    note,
  };
}

/** Members of a combined query (e.g. "orders@recent_query") expand to their own steps. */
export function scriptsFor(step, platform) {
  const ids = step.members?.length ? step.members : [step.id];
  const seen = new Map();
  for (const id of ids) {
    const info = stepScripts(id, platform);
    if (!info) continue;
    const key = info.files.join('|') + info.principle;
    if (!seen.has(key)) seen.set(key, { ...info, notes: info.note ? [info.note] : [] });
    else if (info.note) seen.get(key).notes.push(info.note);
  }
  return [...seen.values()];
}
