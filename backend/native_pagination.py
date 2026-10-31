"""Move a read-only native paginator without replaying earlier result pages."""

import json
from .read_errors import PageReadError


async def move(page, target, previous, family):
    # All controls are scoped to the visible paginator. Never fall back to a
    # page-wide numeric button, which could belong to a filter or an action.
    script = r"""(({target,previous,family})=>{
      const visible=e=>e&&e.offsetWidth>0&&e.offsetHeight>0;
      const enabled=e=>e&&!/disabled/.test(e.className||'')&&e.getAttribute('aria-disabled')!=='true'&&e.getAttribute('data-disabled')!=='true';
      const selector=family==='promotion'?'.anq-pagination':'[data-testid=beast-core-pagination]';
      const p=[...document.querySelectorAll(selector)].find(visible);
      if(!p)return false;
      if(target===previous+1){
        const next=p.querySelector(family==='promotion'?'.anq-pagination-next':'[data-testid=beast-core-pagination-next]');
        if(visible(next)&&enabled(next)){(next.querySelector('button')||next).click();return true;}
      }
      const item=[...p.querySelectorAll('li,button,[role=button],[title],[data-testid]')].find(e=>visible(e)&&enabled(e)&&!e.closest('[role=combobox],[data-testid*=select],.anq-select')&&(e.getAttribute('title')===String(target)||e.textContent.trim()===String(target)));
      if(item){(item.querySelector('button')||item).click();return true;}
      const inputs=[...p.querySelectorAll('input')].filter(e=>visible(e)&&!e.disabled&&!e.readOnly&&!e.closest('[role=combobox],[data-testid*=select],.anq-select'));
      const input=inputs.find(e=>/jump|页|page/i.test((e.parentElement?.className||'')+' '+(e.placeholder||'')))||(inputs.length===1?inputs[0]:null);
      if(!input)return false;
      input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,String(target));
      input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
      for(const type of ['keydown','keypress','keyup'])input.dispatchEvent(new KeyboardEvent(type,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true}));
      return true;
    })"""
    args = json.dumps({"target": target, "previous": previous, "family": family})
    if not await page.evaluate("(" + script + ")(" + args + ")"):
        raise PageReadError(f"历史分页无法定位第 {target} 页，已保留断点和上次完整数据")
