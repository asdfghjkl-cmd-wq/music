# -*- coding: utf-8 -*-
"""dsh 临时脚本:专测**宿主门面的内部数据**能不能被插件直接读走(第四批 facade_slots)。

跑法(项目根目录;`tk.eval` 那几项需要能开 Tk 的环境):

    # 默认:拿当前的 plugin_sandbox.py 测,16/17 应当全部 BLOCK
    & .\\.venv\\Scripts\\python.exe .\\dsh\\facade_probe.py
    # 想对比加固前的实现(副本形式还留着的话):
    $env:FACADE_MODULE = '某个模块名'
    & .\\.venv\\Scripts\\python.exe .\\dsh\\facade_probe.py
    Remove-Item Env:\\FACADE_MODULE

只调用插件命名空间里的一段报告代码,不点菜单、不弹窗。脚本自己开一个
`withdraw()` 过的 Tk 根窗口,跑完就 destroy。
"""

import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULE = os.environ.get('FACADE_MODULE', 'plugin_sandbox').strip()
PLUGIN_DIR = os.path.join(ROOT, 'plugin', 'escape2')

# 这段代码在 box.namespace 里以插件帧(co_filename = frame_tag + 'probe>')执行,
# 所以真 os 的调用会被审计钩子看到 —— 正是我们要观察的。
SRC = '''
p = pro
_g = type(__sandbox__).__init__.__globals__
_os = _g['os']

_ITEMS = [
    ('16  pro.app._app        真 Tkapp',      lambda: p.app._app),
    ('16  pro.app._d          门面内部槽',     lambda: p.app._d),
    ('16b pro._pro            整个宿主对象',   lambda: p._pro),
    ('16b pro._d              门面内部槽',     lambda: p._d),
    ('16c pro.menu._menu      真 Menu 控件',  lambda: p.menu._menu),
    ('16d pro.env_dict        宿主注册表',     lambda: p.env_dict),
    ('17  app._app.tk.eval    Tcl 通道',      lambda: p.app._app.tk.eval('expr 1+1')),
    ('13  真 os.environ       无审计事件',     lambda: _os.environ.get('PATH')),
    ('14  真 os.stat          无审计事件',     lambda: _os.stat('C:\\\\Windows\\\\win.ini').st_size),
    ('--  pro.app.after       白名单应可用',   lambda: p.app.after),
    ('--  pro.app.winfo_width 白名单应可用',   lambda: p.app.winfo_width),
    ('--  pro.menu.add_command 白名单应可用',  lambda: p.menu.add_command),
    ('--  pro.music_dict      白名单应可用',   lambda: p.music_dict),
]

_bad = []
for _label, _fn in _ITEMS:
    try:
        _v, _mark = _fn(), 'OK'
    except Exception as _e:
        _v, _mark = type(_e).__name__, 'BLOCK'
    print('%-5s %-40s %s' % (_mark, _label, repr(_v)[:38]))
    if _mark == 'OK' and not _label.startswith('--'):
        _bad.append(_label)

print()
print('仍然越权的项: %d' % len(_bad))
for _b in _bad:
    print('   ', _b)
'''


def main():
    print(f'[facade-probe] 模块 = {MODULE}')
    try:
        import tkinter
    except Exception:
        print('这个环境没有 tkinter,17 那项测不了')
        return 2

    ps = importlib.import_module(MODULE)

    root = tkinter.Tk()
    root.withdraw()
    menu = tkinter.Menu(root)

    class FakePro:
        pass

    pro = FakePro()
    pro.app = root
    pro.menu = menu
    pro.env_dict = {'secret': 'yes'}          # 插件不该读得到
    pro.music_dict = {'a': 1}
    pro.plugin_list = []

    pol = ps.parse_policy({'fs_read': ['.']}, [], PLUGIN_DIR)
    reg = ps.env_box()
    box = reg.create('facade_probe', 'facade_probe', PLUGIN_DIR, pol,
                     _host_token=ps._HOST_TOKEN)
    box.attach_host(pro)
    ps.install_audit_hook()
    reg.seal()

    try:
        exec(compile(SRC, box.frame_tag + 'probe>', 'exec'), box.namespace)
    finally:
        root.destroy()

    print('--- 审计里记下的 host_attr ---')
    n = 0
    for ev in box.events:
        text = ev if isinstance(ev, str) else getattr(ev, 'detail', None) or str(ev)
        text = str(text)
        if 'host_attr' in text or '不能访问' in text:
            n += 1
            print('   ', text)
    print(f'   (共 {n} 条)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
