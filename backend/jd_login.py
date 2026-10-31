"""Read-only authentication signals from the observed Jingmai merchant shell."""

SNAPSHOT = r"""(()=>{
 const visible=e=>!!e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0&&getComputedStyle(e).visibility!=='hidden';
 const text=document.body?.innerText||'',host=location.hostname;
 const merchant=['shop.jd.com','jingmai.jd.com'].includes(host);
 const name=[...document.querySelectorAll('.shop-menu-accountV1__right-account-top-name,.shop-base__right-title-name')].filter(visible).map(e=>e.textContent.trim()).find(t=>t&&t.length<=100)||'';
 const password=[...document.querySelectorAll('input[type=password]')].some(visible);
 const loginPage=/passport|(^|[/.])login([/.]|$)/i.test(host+location.pathname)||password||/扫码登录|欢迎登录|账户登录|账号密码登录/.test(text)&&!name;
 const verify=[...document.querySelectorAll('[role=dialog],.jd-dialog,.el-dialog,[class*=captcha],[class*=verify-dialog]')].filter(visible).some(e=>/安全验证|拖动.{0,10}滑块|完成.{0,10}拼图|验证码已发送|请验证手机/.test(e.innerText||''));
 const shell=[...document.querySelectorAll('.shop-menu-header')].some(visible);
 const navigation=['商品','交易','财务','店铺'].filter(label=>text.split(/\s+/).includes(label)).length>=3;
 const logged=merchant&&shell&&navigation&&!!name&&!loginPage&&!verify;
 return {logged,name:logged?name:'',loginPage,signedOut:loginPage,verify,failed:loginPage&&/密码错误|账号或密码不正确|账户名或密码错误/.test(text),host};
})()"""
