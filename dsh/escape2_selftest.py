# -*- coding: utf-8 -*-
"""dsh 临时验证脚本:按 b.py 的装载顺序跑 plugin/escape2,看"模块 globals"这条路能不能走出去。

跑法(项目根目录,不用管道 —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\escape2_selftest.py

期望结果:
    * 修复前:escape2 报告里第 06~11 步应当出现 OK(沙盒静默失守);
    * 修复后(plugin_sandbox_fix_module_globals.py 的写法落地之后):
      这些步应当变成 BLOCK,并且留下 violation 审计。

脚本本身只点 escape2 的菜单回调,不点任何 GUI、不弹窗。
"""

import os
import sys
import traceback

# 本脚本住在 dsh/ 里,项目根是它的上一级
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # b.Plugin 用相对路径找 plugin/xxx

# 可选:拿修复副本跑同一套样本,看新路径有没有被堵上(原文件不动)。
#   $env:ESCAPE2_FIX = 'plugin_sandbox_fix_module_globals'
#   & .\.venv\Scripts\python.exe .\dsh\escape2_selftest.py
FIX_MODULE = os.environ.get('ESCAPE2_FIX', '').strip()
if FIX_MODULE:
    import importlib
    sys.modules['plugin_sandbox'] = importlib.import_module(FIX_MODULE)
    print('[escape2] 本次使用修复副本:', FIX_MODULE)

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
    n = b.Plugin(os.path.join('plugin', 'escape2'), reg, set())
    print('插件名 =', n.name, '| env_id =', n.env_id)
    print('装载时策略 =', n.sandbox_policy.describe())
    n.init_env()
    n.init_i(host)
    print('菜单项 =', [c['label'] for c in host.menu.commands])
    # 真实播放器在 main() 里装审计钩子,自测得自己装,否则"真 os"那一层
    # 根本没有第二道闸,会得出错误结论。
    print('审计钩子安装 =', ps.install_audit_hook(),
          '| 已注册插件帧 =', len(ps._FRAME_TAGS))

    box = n._box
    print('--- 按真实播放器:装载完就封存 ---')
    reg.seal()
    print('is_sealed =', box.is_sealed(), '| policy.unsafe =', box.policy.unsafe)

    cmds = {c['label']: c['command'] for c in host.menu.commands}
    report_cmd = next(v for k, v in cmds.items() if '越权报告' in k)
    print('--- 点菜单:"越权报告(模块 globals)" ---')
    res = report_cmd()

    print('--- 沙盒事件(最近的)---')
    for e in box.events[-12:]:
        print('   %-24s %-10s %s' % (e['action'], e['cap'], e['detail'][:70]))

    ns = n._env_dict
    g = type(ns['__sandbox__']).__init__.__globals__

    print('--- 样本跑完之后:内部状态有没有被还原 ---')
    checks = {
        'depth 归零': getattr(g['_FRAME_LOCAL'], 'depth', 0) == 0,
        'under 还是原函数': g['under'].__name__ == 'under',
        '_auth_entry 还是原函数': g['_auth_entry'].__name__ == '_auth_entry',
        '帧注册表还在': len(g['_FRAME_TAGS']) >= 1 and len(g['_FRAME_DIRS']) >= 1,
        '注册记账没被改小': g['_frame_registrar'].count >= 1,
        '越权写探针已删除': not g['os'].path.isfile(r'D:\k\music\.escape2_probe.tmp'),
    }
    for k, ok in checks.items():
        print('   %-24s %s' % (k, 'ok' if ok else '坏掉了'))

    print('--- 状态还原之后:门面 + 审计钩子判定(应当全被拦) ---')
    # 关键:探针必须从**插件代码帧**里发出去。宿主的 dsh 脚本直接调真 os 本来
    # 就不该被审计钩子拦(钩子只在"栈上确实有插件帧"时才动手),所以这一段
    # exec 进插件命名空间,文件名照 b.py 的规矩写成 <plugin 名字 ...>。
    probe = (
        "g = type(__sandbox__).__init__.__globals__\n"
        "verdict = {}\n"
        "def _try(label, thunk):\n"
        "    try:\n"
        "        thunk()\n"
        "    except SandboxDenied as e:\n"
        "        verdict[label] = 'BLOCKED'\n"
        "        print('   拦住 %-30s -> %s' % (label, str(e)[:56]))\n"
        "    except Exception as e:\n"
        "        verdict[label] = 'ERROR'\n"
        "        print('   别的异常 %-26s -> %s' % (label, e))\n"
        "    else:\n"
        "        verdict[label] = 'ALLOWED'\n"
        "        print('   放行 %-30s' % label)\n"
        "_try('门面 open(win.ini)', lambda: open(r'C:\\Windows\\win.ini'))\n"
        "_try('真 os.listdir(C:/Windows)', lambda: g['os'].listdir('C:/Windows'))\n"
        "_try('真 os.system(echo)', lambda: g['os'].system('echo POST-ATTEMPT'))\n"
    )
    code = compile(probe, f'<plugin {n.name} post>', 'exec')
    # 第六批(fake_filename):帧身份改成"code 对象身份"之后,手工 compile 出来的
    # 插件帧也要像真实装载那样先登记 —— 否则这一段会按"宿主代码"判定,钩子不管,
    # 于是"样本跑完之后仍然越权"会误报成放行(实测踩过)。
    _register = getattr(box,'register_plugin_code',None)
    if _register is not None:
        _register(code)
    exec(code, ns)
    verdict = ns.get('verdict', {})

    escaped = list(res['ok'])
    leaked = [k for k, v in verdict.items() if v == 'ALLOWED']

    print('--- 结论 ---')
    print('   样本里越权成功的项:', len(escaped))
    for s in escaped:
        print('      ', s)
    print('   样本跑完后仍然越权的项:', leaked or '无')
    print('   状态还原检查:', '全部 ok' if all(checks.values()) else
          [k for k, v in checks.items() if not v])
    if escaped:
        print('   => 沙盒被拆(模块 globals 这条路成立,bug 复现)')
    else:
        print('   => 沙盒守住了(这条路已被修掉)')
    return 0 if escaped else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
