# -*- coding: utf-8 -*-
"""dsh 临时探针:复现"`bind` 通道没挂回调归属"的越权(bug 名 bind_channel)。

背景
----
第六批(`plugin_sandbox_fix_fake_filename.py`)把"插件帧"的判据从 `co_filename`
字符串改成 **code 对象身份**(`_CodeLedger.code_owner`),并给 `after` / `after_idle`
这条"把回调交给别的执行上下文"的通道挂了**回调归属**(`mark_callback_owner`)。

`_AppFacade._CALLBACK_CHANNELS = ('after','after_idle')` —— 而 `bind` / `bind_all`
在**同一份白名单** `_ALLOW` 里,同一个病没上药:Tk 触发绑定回调时插件帧早就返回,
栈上只剩回调自己那一帧,钩子只能靠"这帧的 code 是不是账本里的插件代码"认人。

`exec` 那条路走不通(门面的 `exec` 是 `sandbox_exec`,顺手就把 code 登记进账本了),
所以要看的是"账本里没有的 code 对象"能不能当回调交出去。本探针测四组:

    A. 插件源码里 def 出来的普通回调                    —— 预期 BLOCK(对照组)
    B. 生成源码 + 门面 `exec`(会自动登记)造的回调      —— 预期 BLOCK
    C. 生成源码 + **真 builtins.exec**(账本看不到)     —— 要测
    D. `FunctionType(真函数的 __code__, globals)` 重组   —— 要测
       (只换 code 对象,连源码都没"生成"过)

判据只看"真进程有没有起来"(`os.system` 造的 marker 文件),不看门面 `open`:
门面那条路本来就拦得住,拿它当判据会得出"沙盒守住了"的错误结论(第六批踩过)。

跑法(项目根目录,**不用管道** —— dsh 下管道会被判成 file access denied):
    .\\.venv\\Scripts\\python.exe .\\dsh\\bind_channel_probe.py
    # 拿副本对比:
    $env:BIND_FIX = 'plugin_sandbox_fix_bind_owner'
    .\\.venv\\Scripts\\python.exe .\\dsh\\bind_channel_probe.py

退出码:0 = 四组全部拦下且宿主未被误伤;2 = 至少一组越权成立(bug 成立);
3 = 拦住了但有误伤(宿主的 bind 回调被一起打死)。
"""

import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # b.Plugin 用相对路径找 plugin/xxx

FIX_MODULE = os.environ.get('BIND_FIX', '').strip()
if FIX_MODULE:
    import importlib
    sys.modules['plugin_sandbox'] = importlib.import_module(FIX_MODULE)
    print('[bind-owner] 本次使用副本:', FIX_MODULE)

import b
import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TMP_DIR = os.path.join(ROOT, '_t_bindchannel')
TMP_PLUGIN = os.path.join(TMP_DIR, 'bindchan')
MARKERS = {k: os.path.join(TMP_PLUGIN, f'.escaped_{k}.tmp') for k in ('A', 'B', 'C', 'D')}

def _raw(path):
    """把路径写成"生成源码里安全的字符串字面量"(用双引号 raw 字符串)。"""
    return '"' + path.replace('\\', '\\\\') + '"'


def _body(marker, tag):
    """生成一个回调函数的源码。路径用 raw 编码,免得 `\\k` 触发 SyntaxWarning
    (告警机制会去 `import warnings`,那是沙盒 import 白名单外的模块)。"""
    return ('def cb(event=None):\n'
            '    _trip(%r,REAL_OS.system(\'cmd /c echo.> %s\'))\n' % (tag, _raw(marker)))


# 四组回调都在**插件帧里**用 `pro.app.bind` 登记(门面放行,也不需要 proc 授权 ——
# `bind` 本身不产生任何审计事件,这正是缺口所在),然后由事件触发。
_PLUGIN_TEMPLATE = '''\
def _g():
    # `__sandbox__` 是 SandboxView,`type(...).__init__.__globals__` 就是
    # plugin_sandbox 模块的字典 —— 这里拿到的是**真** globals(不是门面)。
    return type(__sandbox__).__init__.__globals__

_G = _g()
REAL_OS = _G['os']
REAL_BUILTINS = _G['_builtins']          # 真 builtins(门面的 exec 是 sandbox_exec)
REAL_COMPILE = getattr(REAL_BUILTINS, 'compile')
REAL_EXEC = getattr(REAL_BUILTINS, 'exec')
REAL_FNTYPE = getattr(_G['_types'], 'FunctionType')
BIND = pro.app.bind
_q = _G['box_for_code']

# 每个回调被真的调了几次、`os.system` 返了什么。第九批的修法必须做到"**每一次**
# 触发都算插件",不能只挡第一次(bind 的回调会反复触发,按次数递减的记账会在
# 第二次归零 —— 这正是本 bug 的修法要点)。
TRIPS = []


def _trip(tag,rc):
    TRIPS.append((tag,rc))


def cb_A(event=None):
    # A 对照组:插件源码里的普通函数,code 对象登记过
    _trip('A',REAL_OS.system('cmd /c echo.> @MA@'))


# B:生成源码 + 门面 exec(`sandbox_exec` 会顺手把 code 登记进身份账本)
_BODY_B = @BODY_B@
_ns_b = {'REAL_OS': REAL_OS,'_trip': _trip}
exec(compile(_BODY_B, '<bindchan-facade-exec>', 'exec'), _ns_b)
cb_B = _ns_b['cb']

# C:生成源码 + **真** builtins.exec —— 账本完全看不到这个 code
_BODY_C = @BODY_C@
_code_c = REAL_COMPILE(_BODY_C, '<bindchan-real-exec>', 'exec')
_ns_c = {'REAL_OS': REAL_OS,'_trip': _trip}
REAL_EXEC(_code_c, _ns_c)
cb_C = _ns_c['cb']

# D:FunctionType 重组 —— 不生成源码,只换一个账本里没有的 code 对象
_BODY_D = @BODY_D@
_mod_d = REAL_COMPILE(_BODY_D, '<bindchan-swapped>', 'exec')
cb_D = REAL_FNTYPE(_mod_d.co_consts[1], {'REAL_OS': REAL_OS,'_trip': _trip})

print('[probe] 四个回调就绪:', cb_A, cb_B, cb_C, cb_D)

BIND('<<bindprobe-A>>', cb_A)
BIND('<<bindprobe-B>>', cb_B)
BIND('<<bindprobe-C>>', cb_C)
BIND('<<bindprobe-D>>', cb_D)
print('[probe] 四个回调已 bind 完成;此刻按 code 对象判 C/D =',
      _q(cb_C.__code__), _q(cb_D.__code__))
'''

PLUGIN_SRC = (_PLUGIN_TEMPLATE
              .replace('@BODY_B@', repr(_body(MARKERS['B'], 'B')))
              .replace('@BODY_C@', repr(_body(MARKERS['C'], 'C')))
              .replace('@BODY_D@', repr(_body(MARKERS['D'], 'D')))
              .replace('@MA@', _raw(MARKERS['A'])))
print(PLUGIN_SRC)
PLUGIN_JSON = '''\
{
  "name": "bindchan",
  "can_exec": true,
  "init_file": "a.py",
  "sandbox": {
    "fs_read": ["."],
    "fs_write": [],
    "net": false,
    "proc": false,
    "ask": false,
    "unsafe": false
  }
}
'''


class RealTkHost:
    """最小宿主,但 `app` 是**真的 Tk 根窗口** —— bind 回调和 after 一样是异步的。"""

    def __init__(self, registry):
        import tkinter
        self.app = tkinter.Tk()
        self.app.withdraw()
        self.menu = None
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = registry


def make_plugin():
    if os.path.isdir(TMP_DIR):
        shutil.rmtree(TMP_DIR)
    os.makedirs(TMP_PLUGIN, exist_ok=True)
    with open(os.path.join(TMP_PLUGIN, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write(PLUGIN_SRC)
    with open(os.path.join(TMP_PLUGIN, 'plugin.json'), 'w', encoding='utf-8') as fp:
        fp.write(PLUGIN_JSON)


def main():
    make_plugin()
    for path in MARKERS.values():
        if os.path.isfile(path):
            os.remove(path)

    host = None
    try:
        reg = b.env_box()
        host = RealTkHost(reg)
        n = b.Plugin(TMP_PLUGIN, reg, set())
        print('插件名 =', n.name, '| env_id =', n.env_id)
        n.init_env()
        n.init_i(host)
        print('审计钩子安装 =', ps.install_audit_hook(),
              '| 已注册插件帧 =', len(ps._FRAME_TAGS))
        box = n._box
        reg.seal()

        # 宿主自己也排一组 bind 回调:修复不能把宿主一起当插件打死。
        host_ran = []
        host.app.bind('<<bindprobe-HOST>>', lambda e: host_ran.append(True))

        def fire():
            # mainloop 已经转起来了,这时插件帧早就返回 —— 正是"别的执行上下文"。
            # 每个事件**触发 3 次**:bind 的回调会反复触发,只挡第一次的修法不算修好。
            for _round in range(3):
                for tag in ('A', 'B', 'C', 'D', 'HOST'):
                    try:
                        host.app.event_generate(f'<<bindprobe-{tag}>>')
                    except Exception:
                        traceback.print_exc()
                host.app.update_idletasks()

        host.app.after(80, fire)
        host.app.after(1200, host.app.quit)
        try:
            host.app.mainloop()
        except Exception:
            traceback.print_exc()

        escaped = {k: os.path.isfile(p) for k, p in MARKERS.items()}
        trips = {}
        for item in n._box.namespace.get('TRIPS', ()):
            try:
                tag, rc = item
            except Exception:
                continue
            trips.setdefault(tag, []).append(rc)
        print('--- 结果(真 os.system 有没有造出文件 = 越权成不成立)---')
        print('   A 源码里 def 的普通回调        :', escaped['A'], '(对照组,预期 False)')
        print('   B 生成 + 门面 exec(会登记)     :', escaped['B'], '(预期 False)')
        print('   C 生成 + 真 builtins.exec      :', escaped['C'])
        print('   D FunctionType 换 code 对象    :', escaped['D'])
        print('   宿主自己的 bind 回调执行了     :', bool(host_ran))
        print('--- 每个回调真的被触发的次数 / os.system 的返回码 ---')
        for tag in ('A', 'B', 'C', 'D'):
            got = trips.get(tag, [])
            print('   %s: 触发 %d 次,返回码 %s' % (tag, len(got), got))
        print('--- 沙盒事件(最近的)---')
        for e in box.events[-15:]:
            print('   %-24s %-10s %s' % (e['action'], e['cap'], e['detail'][:72]))
        print('--- 账本 ---')
        print('   code_owner 条目 =', len(ps._ledger.code_owner),
              '| pending =', len(ps._ledger.pending),
              '| _pending_owner =', len(ps._pending_owner))

        bad = [k for k in ('B', 'C', 'D') if escaped[k]]
        if bad:
            print('   => 拦不住:%s 的 bind 回调被当成宿主,第二道闸放行'
                  '(bug 成立:bind_channel)' % bad)
            code = 2
        elif not host_ran:
            print('   => 拦住了,但把宿主自己的 bind 回调也一起拦了(误伤,修复不合格)')
            code = 3
        else:
            print('   => 四组回调全部拦下,宿主未被误伤')
            code = 0
        print('[bind-owner] 退出码 =', code)
        return code
    finally:
        if host is not None:
            try:
                host.app.destroy()
            except Exception:
                pass
        if os.path.isdir(TMP_DIR):
            shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == '__main__':
    try:
        _code = main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        _code = 1
    raise SystemExit(_code)
