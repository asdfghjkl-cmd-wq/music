# -*- coding: utf-8 -*-
"""dsh 临时实验:钩子闭包在注册表被清空后到底还认不认插件帧。"""

import io
import os
import sys

# 本脚本住在 dsh/ 里,项目根是它的上一级
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

PLUGIN_DIR = os.path.join(ROOT, 'plugin', 'escape')
SNIP = r'''
import os as _o
_FAKE_G = globals()
def probe(tag):
    real = _FAKE_G['_fake_real_os']
    try:
        real.listdir(r'D:\k\music')
    except SandboxDenied as e:
        print('   [%s] listdir -> BLOCKED: %s' % (tag, str(e)[:60]))
    except Exception as e:
        print('   [%s] listdir -> %s: %s' % (tag, type(e).__name__, e))
    else:
        print('   [%s] listdir -> ALLOWED' % tag)
_fake_probe = probe
'''


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


def main():
    reg = ps.env_box()
    pol = ps.parse_policy(None, [], PLUGIN_DIR)
    box = reg.create('probe_hook', 'probe_hook', PLUGIN_DIR, pol, _host_token=ps._HOST_TOKEN)
    box.attach_host(FakeHost(reg))
    ps.install_audit_hook()
    reg.seal()
    print('装钩子后: _FRAME_TAGS=%d 项, _FRAME_DIRS=%d 项'
          % (len(ps._FRAME_TAGS), len(ps._FRAME_DIRS)))

    path = os.path.join(PLUGIN_DIR, 'dsh_probe_snip.py')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(SNIP)

    # 用插件文件当文件名,这样帧算"插件帧"
    ns = box.namespace
    ns['_fake_real_os'] = ps.os
    exec(compile(SNIP, path, 'exec'), ns)
    print('--- 清空注册表之前 ---')
    exec(compile('probe("before")', path, 'exec'), ns)

    print('--- 清空注册表 ---')
    ps._FRAME_TAGS.clear()
    del ps._FRAME_DIRS[:]
    print('   清空后: _FRAME_TAGS=%d 项, _FRAME_DIRS=%d 项'
          % (len(ps._FRAME_TAGS), len(ps._FRAME_DIRS)))

    print('--- 清空注册表之后 ---')
    exec(compile('probe("after-clear")', path, 'exec'), ns)

    print('--- 清空后 _box_for_frame 还认得出来吗 ---')
    print('   _box_for_frame(插件文件) =', ps._box_for_frame(path))
    print('   _box_for_frame(<plugin ... init>) =',
          ps._box_for_frame('<plugin probe_hook init>'))

    print('--- 钩子是不是同一个函数对象 ---')
    print('   globals 里的 _audit_hook =', hex(id(ps._audit_hook)))
    print('   globals 里的 _AUDIT_HOOK =', hex(id(ps._AUDIT_HOOK)))
    try:
        cells = ps._AUDIT_HOOK.__closure__
        print('   _AUDIT_HOOK 闭包单元数 =', len(cells))
        for i, cell in enumerate(cells):
            val = cell.cell_contents
            print('      [%d] %s = %r' % (i, type(val).__name__, val if not isinstance(val, (dict, list)) else ('%d 项' % len(val))))
    except Exception as e:
        print('   读闭包失败:', type(e).__name__, e)

    # 注册表被清空后钩子会对所有操作报警(这是我们故意验的)。这里恢复一下,
    # 免得本进程退出时满屏 "Exception ignored in audit hook"。
    ps._FRAME_TAGS[box.frame_tag] = box
    ps._FRAME_DIRS.append((ps._norm(box.plugin_dir),box))
    print('--- 恢复注册表之后 ---')
    exec(compile(SNIP, path, 'exec'), ns)
    exec(compile('probe("restored")', path, 'exec'), ns)

    try:
        os.remove(path)
    except OSError:
        pass


if __name__ == '__main__':
    main()
