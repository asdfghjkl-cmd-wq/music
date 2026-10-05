# -*- coding: utf-8 -*-
"""dsh 临时验证:注册表被清之后,1) 宿主还能不能正常干活,2) 只读白名单还顶不顶用。

跑法(项目根目录):
    .\\.venv\\Scripts\\python.exe .\\dsh\\fix_check.py

三件事:
  A. 插件帧里清空注册表:当场那一次被拒,但宿主**随后**的文件操作必须正常;
  B. 插件把 `_ALWAYS_READABLE` 撑成所有盘根之后,插件读 C:\\Windows\\win.ini 仍要被拦;
  C. 宿主帧在注册表被清后调 os.* 必须照常(播放器不该陪着一起废);
  D. 插件用 object.__setattr__ 硬换 `_policy_frozen`/`can`/`_session` 之后,
     门面与审计钩子仍然按真实策略拒(判权数据在 `_AUTH` 记账里,不在实例上)。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PLUGIN_DIR = os.path.join(ROOT, 'plugin', 'escape')
WIN_INI = r'C:\Windows\win.ini'


class FakeMenu:
    def add_command(self, **kw):
        pass

    def add_separator(self):
        pass


class FakeApp:
    def after(self, ms, fn=None):
        if fn is not None:
            fn()
        return 'id'


class FakeHost:
    def __init__(self, reg):
        self.menu = FakeMenu()
        self.app = FakeApp()
        self.music_dict = {}
        self.plugin_list = []
        self.env_dict = reg


# 插件帧里跑这段:先清注册表,再试着越权读
PLUGIN_SNIP = r'''
g = __sandbox__.can.__self__                  # 实例属性是绑定方法,__self__ 就是 SandBox
gg = type(g).deny.__globals__
tags = gg['_FRAME_TAGS']
dirs = gg['_FRAME_DIRS']
tags.clear()
del dirs[:]
print('   [plugin] 已清空注册表: _FRAME_TAGS=%d _FRAME_DIRS=%d' % (len(tags), len(dirs)))
# A: 这一次操作必须被拒
try:
    with open(r'D:\k\music\b.py', encoding='utf-8') as fp:
        fp.read(10)
except SandboxDenied as e:
    print('   [plugin] 清表后读项目文件 -> BLOCKED:', str(e)[:50])
else:
    print('   [plugin] 清表后读项目文件 -> ALLOWED (不行)')
print('   [plugin] 退出前: _FRAME_TAGS=%d _FRAME_DIRS=%d' % (len(tags), len(dirs)))
'''


# 插件帧里跑这段:只撑大只读白名单(不动注册表),再读 win.ini
WHITELIST_SNIP = r'''
g = __sandbox__.can.__self__
gg = type(g).deny.__globals__
gg['_ALWAYS_READABLE'] = ('C:\\', 'D:\\', 'E:\\', 'F:\\')
print('   [plugin] 已撑大 _ALWAYS_READABLE =', gg['_ALWAYS_READABLE'])
try:
    with open(r'C:\Windows\win.ini', encoding='utf-8', errors='replace') as fp:
        fp.read(20)
except SandboxDenied as e:
    print('   [plugin] 撑白名单后读 win.ini -> BLOCKED:', str(e)[:50])
else:
    print('   [plugin] 撑白名单后读 win.ini -> ALLOWED (不行!)')
'''

# 插件帧里跑这段:硬来换判权数据/判权入口(内省 + object.__setattr__ 绕过守卫),
# 然后看判权还认不认它。第二批加固前这里能拿回整个 C 盘。
HIJACK_SNIP = r'''
g = __sandbox__.can.__self__
print('   [plugin] 硬来:换 _policy_frozen / can / _session')
try:
    snap = g.policy.snapshot()
    object.__setattr__(snap, 'unsafe', True)
    object.__setattr__(snap, 'fs_read', (r'C:\\',))
    object.__setattr__(snap, 'fs_write', (r'C:\\',))
    object.__setattr__(g, '_policy_frozen', snap)          # 绕过 guarded_names
    print('   [plugin]   _policy_frozen 已被换掉(硬来成功)')
except Exception as e:
    print('   [plugin]   换 _policy_frozen 失败:', type(e).__name__, str(e)[:40])
try:
    object.__setattr__(g, 'can', lambda cap, target=None: True)
    print('   [plugin]   box.can 已被换掉(硬来成功)')
except Exception as e:
    print('   [plugin]   换 box.can 失败:', type(e).__name__, str(e)[:40])
try:
    g._session['fs:write'].add(r'C:\\')
    print('   [plugin]   _session 里也塞了 C:\\')
except Exception as e:
    print('   [plugin]   塞 _session 失败:', type(e).__name__, str(e)[:40])
# 两道闸都必须拒:门面(它的检查现在走 _auth_can)和真 os(审计钩子)
for _what, _fn in (('门面 open', lambda: open(r'C:\Windows\win.ini').read(3)),
                   ('真 builtins.open', lambda: __import__('builtins').open(
                       r'C:\Windows\win.ini').read(3))):
    try:
        _fn()
    except SandboxDenied as e:
        print('   [plugin] 硬来之后用 %s 读 win.ini -> BLOCKED: %s' % (_what, str(e)[:40]))
    except Exception as e:
        print('   [plugin] 硬来之后用 %s -> 别的异常:' % _what, type(e).__name__, str(e)[:40])
    else:
        print('   [plugin] 硬来之后用 %s 读 win.ini -> ALLOWED (不行!)' % _what)
'''


def main():
    reg = ps.env_box()
    pol = ps.parse_policy(None, [], PLUGIN_DIR)
    box = reg.create('fixcheck', 'fixcheck', PLUGIN_DIR, pol, _host_token=ps._HOST_TOKEN)
    box.attach_host(FakeHost(reg))
    ps.install_audit_hook()
    reg.seal()
    print('装钩子后注册表: _FRAME_TAGS=%d' % len(ps._FRAME_TAGS))

    path = os.path.join(PLUGIN_DIR, 'dsh_fix_probe.py')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(PLUGIN_SNIP)
    print('--- 插件帧里跑 ---')
    try:
        exec(compile(PLUGIN_SNIP, path, 'exec'), box.namespace)
    except ps.SandboxDenied as e:
        print('   [plugin] 直接抛出来了:', str(e)[:60])
    except Exception as e:
        print('   [plugin] 别的异常:', type(e).__name__, e)

    print('--- B. 只撑大只读白名单(不动注册表) ---')
    try:
        exec(compile(WHITELIST_SNIP, path, 'exec'), box.namespace)
    except ps.SandboxDenied as e:
        print('   [plugin] 直接抛出来了:', str(e)[:60])
    except Exception as e:
        print('   [plugin] 别的异常:', type(e).__name__, e)
    # 把白名单还原,免得干扰下面"宿主干活"的检查
    ps._ALWAYS_READABLE = tuple(
        p for p in (ps._norm(sys.prefix), ps._norm(sys.base_prefix),
                    ps._norm(os.path.dirname(os.__file__))) if p)
    print('   白名单已还原 =', ps._ALWAYS_READABLE)

    print('--- D. 硬来换判权数据/判权入口(object.__setattr__ 绕过守卫) ---')
    try:
        exec(compile(HIJACK_SNIP, path, 'exec'), box.namespace)
    except ps.SandboxDenied as e:
        print('   [plugin] 直接抛出来了:', str(e)[:60])
    except Exception as e:
        print('   [plugin] 别的异常:', type(e).__name__, e)

    print('--- C. 宿主帧(注册表刚被清过)能不能正常干活 ---')
    try:
        with open(os.path.join(ROOT, 'b.py'), encoding='utf-8') as fp:
            head = fp.read(20)
        print('   宿主读 b.py -> OK', repr(head[:16]))
    except Exception as e:
        print('   宿主读 b.py -> 坏了:', type(e).__name__, str(e)[:70])
    try:
        with open(os.path.join(ROOT, 'dsh', 'host_write_probe.txt'), 'w',
                  encoding='utf-8') as fp:
            fp.write('ok')
        os.remove(os.path.join(ROOT, 'dsh', 'host_write_probe.txt'))
        print('   宿主写文件 -> OK')
    except Exception as e:
        print('   宿主写文件 -> 坏了:', type(e).__name__, str(e)[:70])
    print('   注册表现在: _FRAME_TAGS=%d _FRAME_DIRS=%d' % (len(ps._FRAME_TAGS), len(ps._FRAME_DIRS)))

    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == '__main__':
    main()
