# -*- coding: utf-8 -*-
"""dsh 临时验证脚本:复现 plugin/test-1/a.py 的"伪造 co_filename + app.after"逃逸。

为什么必须用**真的 Tk 主循环**:
    escape2 第 12/13 步里,第 13 步(伪造名 + `pro.app.after`)在所有夹具里都只能
    记 N/A —— 因为 `FakeApp.after` 是**同步**调用的:回调仍在插件的调用链上,栈上
    外层那个真插件帧还在,审计钩子照样认得出是谁干的。真机上是异步的:
    `after` 只把回调塞进 Tcl 的定时器队列,等 mainloop 下一轮才执行,那时插件帧
    早就返回了 —— 栈上只剩伪造文件名的那一帧。本脚本用真的 `tkinter.Tk()` 复现这一点。

跑法(项目根目录,不用管道 —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\fake_filename_selftest.py

不设 `ESCAPE2_FIX` 跑的就是当前 `plugin_sandbox.py`(第六批的修复已并进正式文件),
现在应当 exit 0("拦住了伪造帧,而且宿主未被误伤");要看**加固前**的 exit 2,
把 `ESCAPE2_FIX` 指到没有第六批加固的实现即可。

测试用插件是脚本**临时生成**的(`_t_fakeframe/`,跑完删掉),不往 plugin/ 里加东西:
它的形状和 plugin/test-1/a.py 一模一样,只是把 `taskkill` 换成两条无害探针
(真 `os.system('echo ...')` + 真 `open(探针,'w')`),否则杀进程跑不了自测。

判据(两件事都得看):
    * 逃逸是否成立:`os.system` 的 marker 有没有出现在输出里、探针文件有没有被写出来;
    * 宿主是否被误伤:同一轮 mainloop 里**宿主**自己排的 after 回调必须照常执行
      (修复如果做成"after 回调一律当插件",宿主就被连坐了)。

退出码:0 = 逃逸没成立且宿主没被误伤;2 = 逃逸成立;3 = 拦住了但误伤宿主。
"""

import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # b.Plugin 用相对路径找 plugin/xxx

FIX_MODULE = os.environ.get('ESCAPE2_FIX', '').strip()
if FIX_MODULE:
    import importlib
    sys.modules['plugin_sandbox'] = importlib.import_module(FIX_MODULE)
    print('[fake-frame] 本次使用副本:', FIX_MODULE)

import b
import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

TMP_DIR = os.path.join(ROOT, '_t_fakeframe')
TMP_PLUGIN = os.path.join(TMP_DIR, 'fakeframe')
PROBE = os.path.join(TMP_PLUGIN, '.fake_frame_probe.tmp')
MARKER = 'FAKE-FRAME-ESCAPED'
MARKER_FILE = os.path.join(TMP_PLUGIN, MARKER)

# 形状照抄 plugin/test-1/a.py(伪造 co_filename + 交给 app.after 异步执行),
# 只把 payload 换成无害探针:`os.system` 换成 `cmd /c echo.> marker`,破坏性的
# `taskkill /f /im python.exe` 不跑。
#
# 为什么只留 os.system 这一条探针:
#   * 它走的是"真 os"(内省拿到的),不经过门面 —— 探针文件由 cmd 自己写,
#     所以"文件在不在"就是"真进程有没有起来"的铁证;
#   * 走门面 `open(...,'w')` 那条是**假逃生**:门面拦得住它(`check_fs` 当场
#     SandboxDenied),拦住的是门面这一层,跟伪造帧能不能骗过审计钩子是两回事。
#     拿它当判据会得出"沙盒守住了"的错误结论(实测踩过这个坑)。
PLUGIN_SRC = '''\
def _globals():
    return type(__sandbox__).__init__.__globals__
g = _globals()
src = (
        "real = PAYLOAD['os']\\n"
        "def cb():\\n"
        "    real.system('cmd /c echo.> ' + chr(34) + PAYLOAD['markerfile'] + chr(34))\\n"
    )
ns = {'PAYLOAD': {'os': g['os'], 'markerfile': r'@MARKERFILE@'}}
try:
        exec(compile(src, '<not-a-plugin>', 'exec'), ns)
        cb = ns['cb']                      # 这个函数的 co_filename 是 <not-a-plugin>
        pro.app.after(0, cb)
        g['time'].sleep(0.2)
except Exception:
    pass
'''

PLUGIN_SRC = PLUGIN_SRC.replace('@MARKERFILE@', MARKER_FILE)

PLUGIN_JSON = '''\
{
  "name": "fake_frame_probe",
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


class _Tee:
    """把 stdout 复制一份,好在 mainloop 之后查 marker 有没有被 echo 出来。

    注意:marker 不靠 stdout 判定 —— 被重定向时 Python 的 stdout 是块缓冲,
    而 `os.system` 的 `echo` 走的是继承来的控制台句柄,两条路不同步。
    所以 marker 一律用 `os.system` 写出来的**文件**当证据(见 PROBE)。
    """

    def __init__(self, real):
        self.real = real
        self.buf = []

    def write(self, s):
        self.buf.append(s)
        return self.real.write(s)

    def flush(self):
        return self.real.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


class RealTkHost:
    """最小宿主:只给门面需要的东西,但 `app` 是**真的 Tk 根窗口**。

    `pro.app.after` 走的就是 tkinter 的真 `after` —— 异步,这正是逃逸的立足点。
    """

    def __init__(self, registry):
        import tkinter
        self.app = tkinter.Tk()
        self.app.withdraw()                  # 别弹窗,自测不需要界面
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
    if os.path.isfile(PROBE):
        os.remove(PROBE)

    tee = _Tee(sys.stdout)
    sys.stdout = tee

    host = None
    try:
        reg = b.env_box()
        host = RealTkHost(reg)
        n = b.Plugin(TMP_PLUGIN, reg, set())
        print('插件名 =', n.name, '| env_id =', n.env_id)
        print('装载时策略 =', n.sandbox_policy.describe())
        n.init_env()
        n.init_i(host)                        # 里面用 app.after(0, _run_init) 投递
        print('审计钩子安装 =', ps.install_audit_hook(),
              '| 已注册插件帧 =', len(ps._FRAME_TAGS))

        box = n._box
        reg.seal()
        print('is_sealed =', box.is_sealed())

        # 宿主自己排一个 after 回调:修复不能把宿主也一起当插件拦掉。
        host_ran = []
        host.app.after(60, lambda: host_ran.append(True))

        # 真 Tk 主循环:插件的 cb 在 mainloop 之后才执行,那时插件的 init 帧
        # 早已返回 —— 栈上只剩 `<not-a-plugin>` 那一帧。
        host.app.after(1200, host.app.quit)
        try:
            host.app.mainloop()
        except Exception:
            traceback.print_exc()

        system_ran = os.path.isfile(MARKER_FILE)
        print('--- 结果 ---')
        print('   真 os.system 造出 marker 文件(proc 越权):', system_ran,
              '->', MARKER_FILE)
        print('   宿主自己的 after 回调执行了:', bool(host_ran))
        print('--- 沙盒事件(最近的)---')
        for e in box.events[-12:]:
            print('   %-24s %-10s %s' % (e['action'], e['cap'], e['detail'][:70]))

        # 只有"真进程起来"才算逃逸:门面 open 那条路拦得住,不能当判据。
        escaped = system_ran
        if escaped:
            print('   => 拦不住:`<not-a-plugin>` 帧被当成宿主,第二道闸放行(bug 成立)')
            return 2
        if not host_ran:
            print('   => 拦住了,但把宿主的 after 回调也一起拦了(误伤,修复不合格)')
            return 3
        print('   => 拦住了伪造帧,而且宿主未被误伤')
        return 0
    finally:
        sys.stdout = tee.real
        if host is not None:
            try:
                host.app.destroy()
            except Exception:
                pass
        if os.path.isdir(TMP_DIR):
            shutil.rmtree(TMP_DIR, ignore_errors=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
