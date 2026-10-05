# -*- coding: utf-8 -*-
"""dsh 临时验证脚本:按 b.py 的装载顺序跑 plugin/escape,看逃逸插件能不能走出去。

跑法(项目根目录):
    & .\\.venv\\Scripts\\python.exe .\\dsh_escape_selftest.py

期望结果:
    * 修复前:第 2 步 escape() -> True,第 3 步读 C:\\Windows\\win.ini 成功、能起进程;
    * 修复后:escape() 应该拿不到可用凭据 / 拆不动沙盒,越权操作继续被拦。
"""

import os
import sys
import traceback

# 本脚本住在 dsh/ 里,项目根是它的上一级
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # b.Plugin 用相对路径找 plugin/xxx

import b
import plugin_sandbox as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

OUTSIDE = r'C:\Windows\win.ini'


class FakeMenu:
    def __init__(self):
        self.commands = []

    def add_command(self, **kw):
        self.commands.append(kw)

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
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = reg


def main():
    reg = b.env_box()
    host = FakeHost(reg)
    n = b.Plugin(os.path.join('plugin', 'escape'), reg, set())
    print('插件名 =', n.name, '| env_id =', n.env_id)
    print('装载时策略 =', n.sandbox_policy.describe())
    n.init_env()
    n.init_i(host)
    print('菜单项 =', [c['label'] for c in host.menu.commands])
    # 真实播放器是在 main() 里装审计钩子的,自测得自己装,否则"真 os"那一层
    # 根本没有第二道闸,会得出错误结论。
    print('审计钩子安装 =', ps.install_audit_hook(),
          '| 已注册插件帧 =', len(ps._FRAME_TAGS))

    box = n._box
    print('--- 按真实播放器:装载完就封存 ---')
    reg.seal()
    print('is_sealed =', box.is_sealed(), '| policy.unsafe =', box.policy.unsafe)

    report_cmd = [c for c in host.menu.commands if '完整报告' in c['label']][0]['command']
    print('--- 点菜单:"完整报告(撞墙/逃逸/走出去)" ---')
    report_cmd()

    print('--- 沙盒事件 ---')
    for e in box.events:
        print('   %-22s %-10s %s' % (e['action'], e['cap'], e['detail'][:80]))

    ns = n._env_dict
    print('--- 逃逸尝试之后:门面 + 审计钩子判定 ---')
    verdict = {}
    for label, thunk in (
        ('open(%s)' % OUTSIDE, lambda: ns['open'](OUTSIDE)),
        ('os.listdir(D:\\k\\music)', lambda: ns['os'].listdir(r'D:\k\music')),
        ('os.system(echo)', lambda: ns['os'].system('echo POST-ATTEMPT')),
        ('__import__(ctypes)', lambda: ns['__builtins__']['__import__']('ctypes')),
        ('os.environ', lambda: ns['os'].environ),
    ):
        try:
            thunk()
        except ps.SandboxDenied as e:
            verdict[label] = 'BLOCKED'
            print('   拦住 %-32s -> %s' % (label, str(e)[:70]))
        except Exception as e:
            verdict[label] = 'ERROR'
            print('   别的异常 %-28s -> %s' % (label, e))
        else:
            verdict[label] = 'ALLOWED'
            print('   放行 %-32s' % label)

    print('--- 结论 ---')
    print('   policy.unsafe =', box.policy.unsafe)
    print('   策略 =', box.policy.describe())
    print('   can(fs:read, %s) = %s' % (OUTSIDE, ns['__sandbox__'].can('fs:read', OUTSIDE)))
    leaked = [k for k, v in verdict.items() if v == 'ALLOWED']
    print('   越权成功的项:', leaked or '无')

    # 判权与审计入口的"封存后不可再绑"是第二批加固的核心,单独断言一遍:
    # 只报"越权成功的项: 无"还不够 —— 万一以后有人把 can/violation 又改回
    # 普通实例属性,那几个探针被挡住的原因可能变成别的,这条会先炸。
    print('   --- 判权/审计入口(封存后不可改写)---')
    hijack = []
    for name in ('can', 'violation', 'note', 'events', 'policy', '_session',
                 '_policy_frozen', '_org'):
        try:
            setattr(box, name, None)
        except ps.SandboxDenied as e:
            print('   %-16s 改不动:%s' % (name, str(e)[:52]))
        except Exception as e:
            hijack.append('%s 换了个异常:%r' % (name, e))
        else:
            hijack.append(name + ' 竟然被改掉了')
    # 第 09 步换掉的 violation 必须还在记事后审计
    print('   审计还记不记事:', '是' if len(box.events) > 4 else '否(被静音了!)')
    if hijack:
        print('   还有入口能被改写:', hijack)
    print('   => 逃逸' + ('成功(沙盒仍可被拆)' if (leaked or hijack) else '失败(沙盒修复生效)'))
    return 0 if not leaked and not hijack else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
