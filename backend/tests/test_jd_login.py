import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from backend.jd_login import SNAPSHOT
from backend import platform_login
from backend.browser import Session
from backend.store import Store


class JdSnapshot(unittest.TestCase):
    def test_live_dom_shapes_and_challenges(self):
        # Exercise the actual browser classifier against JD's observed DOM shape.
        script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const source=JSON.parse(process.argv[1]);
function classify(options={}){
 const name={textContent:'测试旗舰店',getBoundingClientRect:()=>({width:100,height:20})};
 const dialog={innerText:'安全验证 请完成拼图',getBoundingClientRect:()=>({width:options.hidden?0:300,height:200})};
 const ctx={location:{hostname:options.host||'shop.jd.com',pathname:options.login?'/login':'/jdm/home'},getComputedStyle:()=>({visibility:'visible'}),document:{body:{innerText:options.login?'欢迎登录 账号密码登录':'首页 商品 交易 财务 店铺 经营数据 安全验证帮助'},querySelectorAll:selector=>selector.includes('account-top-name')?(options.noName?[]:[name]):selector==='input[type=password]'?(options.login?[name]:[]):selector==='.shop-menu-header'?[name]:options.verify?[dialog]:[]}};
 return vm.runInNewContext(source,ctx);
}
assert.equal(classify().logged,true);
assert.equal(classify().name,'测试旗舰店');
assert.equal(classify({login:true}).logged,false);
assert.equal(classify({verify:true}).verify,true);
assert.equal(classify({verify:true}).logged,false);
assert.equal(classify({verify:true,hidden:true}).logged,true);
assert.equal(classify({noName:true}).logged,false);
assert.equal(classify({host:'passport.jd.com'}).logged,false);
assert.equal(classify({host:'shop.jd.com.example.com'}).logged,false);
"""
        node = Path(__file__).resolve().parents[2] / ".runtime/node.exe"
        result = subprocess.run(
            [str(node), "-e", script, json.dumps(SNAPSHOT)], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class JdState(unittest.IsolatedAsyncioTestCase):
    async def test_verified_shop_recovers_and_preserves_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.add("jd-account", "password", platform="jd")
            session = Session(store.list()[0], store)
            session.shop["alias"] = "自定义店铺名称"
            session.state = "needs_verification"
            session.recovery_status = "请扫码"
            session.auto_window = AsyncMock()
            session.evaluate = AsyncMock(return_value={"logged": True, "name": "京东旗舰店", "verify": False})
            await platform_login.refresh(session)
            session.evaluate.assert_awaited_once_with(SNAPSHOT)
            self.assertEqual(session.state, "logged_in")
            self.assertTrue(session.authenticated)
            self.assertEqual(session.recovery_status, "")
            self.assertEqual(session.shop["name"], "自定义店铺名称")
            self.assertEqual(store.list()[0]["detected_name"], "京东旗舰店")
            session.auto_window.assert_awaited_once_with(False)
            session.evaluate.return_value = {"logged": False, "name": "", "verify": True}
            await platform_login.refresh(session)
            self.assertEqual(session.state, "needs_verification")
            self.assertFalse(session.authenticated)
