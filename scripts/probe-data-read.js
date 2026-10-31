(async () => {
  const jobs = [
    [
      '/vodka/v2/mms/query/display/mall/goodsList',
      { page: 1, out_goods_sn_gray_flag: true, pre_sale_type: 4, shipment_time_type: 3, size: 10 },
    ],
    [
      '/strickland/sop/mms/todoList',
      {
        serviceStatus: [null],
        problemType: [null],
        createStartTime: Math.floor(Date.now() / 1000) - 7 * 86400,
        createEndTime: Math.floor(Date.now() / 1000),
        pageNum: 1,
        pageSize: 50,
      },
    ],
    [
      '/saturn/reviews/list',
      {
        startTime: Math.floor(Date.now() / 1000) - 3 * 86400,
        endTime: Math.floor(Date.now() / 1000),
        pageNo: 1,
        pageSize: 10,
        orderSn: '',
      },
    ],
  ];
  const result = [];
  for (const [url, body] of jobs) {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    const a = d.result;
    result.push({
      url,
      status: r.status,
      success: d.success,
      error: d.errorMsg,
      error_code: d.error_code,
      error_msg: d.error_msg,
      errorCode: d.errorCode,
      total: a?.total ?? a?.totalNum,
      count: (a?.goods_list ?? a?.dataList ?? a?.data ?? []).length,
    });
  }
  return result;
})();
