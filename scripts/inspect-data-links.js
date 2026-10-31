(() => ({
  links: [...document.querySelectorAll('a[href]')]
    .filter((a) => ['商品列表', '评价管理', '工单管理'].includes(a.innerText.trim()))
    .map((a) => ({ text: a.innerText.trim(), url: a.href })),
}))();
