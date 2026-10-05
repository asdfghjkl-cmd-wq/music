# -*- coding: utf-8 -*-
"""第六批(fake_filename)的对抗性探针:修完之后还有哪些"甩上下文"的路子。

不是回归夹具,是**自证边界**用的:
  A. after 门面(真 Tk,异步):伪造帧应当被拦 —— 这是本批修的主路;
  B. 内省真 Tkapp 的 Tcl 通道(`.tk`),把伪造回调注册成 Tcl 命令、由**外面**
     在插件帧早已结束后触发:同样应当被拦(钩子按 code 身份/登记认人);
  C. 门面 `bind` 通道:预期仍然通不了(门面没放行 event_generate,插件自己没法
     触发事件),如实报告。

跑法(项目根目录):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\fake_filename_probe.py
"""

import importlib
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

FIX = os.environ.get('ESCAPE2_FIX', 'plugin_sandbox').strip()
sys.modules['plugin_sandbox'] = importlib.import_module(FIX)
import b
import plugin_sandbox as ps

import tkinter

TMP = os.path.join(ROOT, '_t_ffprobe')
PLUG = os.path.join(TMP, 'p')
MARK = os.path.join(PLUG, 'MARKER.txt')

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 公共前半段:内省拿真 os,把真正的动作编成一棵伪造文件名的 code 树
HEAD = '''
g = type(__sandbox__).__init__.__globals__
src = (
    "real = PAYLOAD['os']\\n"
    "def cb(*a):\\n"
    "    real.system('cmd /c echo.> ' + chr(34) + PAYLOAD['m'] + chr(34))\\n"
)
ns = {'PAYLOAD': {'os': g['os'], 'm': r'@MARK@'}}
exec(compile(src, '<not-a-plugin>', 'exec'), ns)
'''

# A:交给**宿主门面**(真 Tk)的 after —— 异步,插件帧早已返回
A = HEAD + '''
pro.app.after(0, ns['cb'])
'''

# B:绕过门面,直接拿真 Tcl 通道,把回调注册成 Tcl 命令;由探针在外面触发
B = HEAD + '''
root = pro.app
# 第四批遗留:门面的 _d 槽能用"类描述符"读走真 Tkapp(SANDBOX.md"已知的坑")
slot = '_' + 'd'
real = type(root).__dict__[slot].__get__(root)[1]
tk = real.tk
tk.createcommand('ffcb1', ns['cb'])
_ff_tk = tk                                # 交给探针在外面触发(模拟 mainloop 的下一轮)
'''

# C:门面白名单里的 bind(回调挂到窗口事件上)
C = HEAD + '''
pro.app.bind('<Button-1>', ns['cb'])
'''


class Host:
    def __init__(self, reg):
        self.app = tkinter.Tk()
        self.app.withdraw()
        self.menu = None
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = reg


def make(src):
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(PLUG)
    with open(os.path.join(PLUG, 'a.py'), 'w', encoding='utf-8') as fp:
        fp.write(src.replace('@MARK@', MARK))
    with open(os.path.join(PLUG, 'plugin.json'), 'w', encoding='utf-8') as fp:
        fp.write('{"name":"ffprobe","can_exec":true,"init_file":"a.py",'
                 '"sandbox":{"fs_read":["."],"fs_write":[],"net":false,'
                 '"proc":false,"ask":false,"unsafe":false}}')


def load():
    """照 b.py 的装载顺序跑一遍,返回 (host, plugin)。"""
    reg = b.env_box()
    host = Host(reg)
    n = b.Plugin(PLUG, reg, set())
    n.init_env()
    n.init_i(host)                 # init 由 app.after(0,_run_init) 投递
    ps.install_audit_hook()
    reg.seal()
    host.app.after(150, host.app.quit)
    try:
        host.app.mainloop()        # 插件帧在这里面跑完并退出
    except Exception:
        traceback.print_exc()
    return host, n


def leaked():
    got = os.path.isfile(MARK)
    if got:
        os.remove(MARK)
    return got


def try_second_loop(host, trigger):
    """插件帧已经彻底结束之后,再开一轮 mainloop 触发回调。"""
    try:
        trigger()
    except Exception:
        traceback.print_exc()
        return False
    host.app.after(200, host.app.quit)
    try:
        host.app.mainloop()
    except Exception:
        traceback.print_exc()
    return True


def main():
    print('[ffprobe] 实现 =', FIX)
    results = {}

    # A:门面 after(异步)
    make(A)
    host, n = load()
    # A 的回调排的是 after(0,...):第一轮 mainloop 里就该跑掉了
    results['A after 门面(异步,带登记)'] = leaked()
    try:
        host.app.destroy()
    except Exception:
        pass

    # B:真 Tcl 通道,插件帧结束后由外面触发
    make(B)
    host, n = load()
    tk = n._env_dict.get('_ff_tk')     # B 段把真 tk 放这儿,交给探针在外面触发
    if tk is None:
        traceback.print_exc()
        results['B 真 Tcl 通道(插件帧外触发)'] = None
    else:
        try_second_loop(host, lambda: tk.call('after', 0, 'ffcb1'))
        results['B 真 Tcl 通道(插件帧外触发)'] = leaked()
    try:
        host.app.destroy()
    except Exception:
        pass

    # C:门面 bind,插件自己没法触发事件 => 记 N/A(不是"守住了")
    make(C)
    host, n = load()
    results['C 门面 bind(插件自己触发不了)'] = None
    try:
        host.app.destroy()
    except Exception:
        pass

    shutil.rmtree(TMP, ignore_errors=True)
    print('--- 汇总 ---')
    for k, v in results.items():
        print(f'   {k}: {"LEAK(越权成功)" if v else ("BLOCK" if v is False else "N/A")}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
