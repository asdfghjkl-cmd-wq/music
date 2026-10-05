# -*- coding: utf-8 -*-
"""dsh 验证脚本(修订版):按 b_fixed 的装载顺序跑 plugin/escape2,检验"模块 globals"这条路。

这是 `dsh/escape2_selftest.py` 的修订副本(原文件未改动)。两处修复:

1. **判定逻辑** —— 原版 `escaped = list(res['ok'])` 把 escape2 报告里所有 OK 都算作
   "越权成功",于是「01 内省拿回 SandBox」和「02 偷 _HOST_TOKEN」这两步**纯侦察**
   (脚本自己都写着"拿到也未必能用")被计入 escape,导致在**零真实逃逸**的情况下
   打印「=> 沙盒被拆(模块 globals 这条路成立,bug 复现)」,结论与自身测量相反。
   本版把侦察步骤单列,只有真正取得越权能力(解除限制 / 读出目录外文件 / 起进程)
   才算逃逸。

2. **状态检查** —— 原版检查 `_FRAME_LOCAL.depth` 是否归零。该字段在修订版沙盒里
   已被删除(它只写不读,是第二批 `_facade_enter/_facade_exit` 的遗留死状态)。
   本版改为断言死状态确实已移除。

跑法(项目根目录,**不用管道** —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\dsh\\escape2_selftest_fixed.py

本脚本只点 escape2 的菜单回调,不点任何 GUI、不弹窗。
"""

import os
import sys
import traceback

# 本脚本住在 dsh/ 里,项目根是它的上一级
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)                      # Plugin 用相对路径找 plugin/xxx

# 直接指向修订副本:不做 sys.modules 替换 —— 那会让 `from plugin_sandbox.
# plugin_sandboxa_fixed import ...` 找不到子模块(替换后的对象没有 __path__)。
import b as b
import plugin_sandbox.plugin_sandboxa as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

OUTSIDE = r'C:\Windows\win.ini'

# 这两步只是**侦察**:拿到对象/凭据不等于取得越权能力。
# 凭据是否可用由样本的第 03/04 步单独验证(实测均为 BLOCK)。
RECON_PREFIXES = ('01 ', '02 ')


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
        # 注意:escape2 的第 06 步自己就会往 _FRAME_LOCAL 上写 depth,所以
        # "属性不存在"不能当判据(第一版就写错了)。真正要证明的是**沙盒侧读
        # depth 的那段死代码已经删掉** —— 现在置位 depth 再也拦不住钩子,
        # 第 06 步实测 BLOCK 就是这条的证据。
        '死状态代码已移除(置位 depth 不再影响判权)':
            '_facade_enter' not in g and '_facade_exit' not in g,
        'under 还是原函数': g['under'].__name__ == 'under',
        '_auth_entry 还是原函数': g['_auth_entry'].__name__ == '_auth_entry',
        '帧注册表还在': len(g['_FRAME_TAGS']) >= 1 and len(g['_FRAME_DIRS']) >= 1,
        '注册记账没被改小': g['_frame_registrar'].count >= 1,
        '越权写探针已删除': not g['os'].path.isfile(os.path.join(ROOT, '.escape2_probe.tmp')),
    }
    for k, ok in checks.items():
        print('   %-24s %s' % (k, 'ok' if ok else '坏掉了'))

    print('--- 状态还原之后:门面 + 审计钩子判定(应当全被拦) ---')
    # 关键:探针必须从**插件代码帧**里发出去。宿主的 dsh 脚本直接调真 os 本来
    # 就不该被审计钩子拦(钩子只在"栈上确实有插件帧"时才动手),所以这一段
    # exec 进插件命名空间,文件名照装载器的规矩写成 <plugin 名字 ...>。
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
    # 帧身份是"code 对象身份"之后,手工 compile 出来的插件帧也要像真实装载那样
    # 先登记 —— 否则这一段会按"宿主代码"判定,钩子不管,于是"样本跑完后仍然越权"
    # 会误报成放行(实测踩过)。
    _register = getattr(box, 'register_plugin_code', None)
    if _register is not None:
        _register(code)
    exec(code, ns)
    verdict = ns.get('verdict', {})

    all_ok = list(res['ok'])
    recon = [s for s in all_ok if s.startswith(RECON_PREFIXES)]
    # 真正的逃逸 = 除侦察之外,还拿到 OK 的步骤
    escaped = [s for s in all_ok if not s.startswith(RECON_PREFIXES)]
    leaked = [k for k, v in verdict.items() if v == 'ALLOWED']

    print('--- 结论 ---')
    print('   侦察成功(不算逃逸):', len(recon))
    for s in recon:
        print('      ', s)
    print('   真正越权成功的项:', len(escaped))
    for s in escaped:
        print('      ', s)
    print('   样本跑完后仍然越权的项:', leaked or '无')
    print('   状态还原检查:', '全部 ok' if all(checks.values()) else
          [k for k, v in checks.items() if not v])

    # 机器可读结论:写到系统临时目录(和 tests/*_fixed.py 一个做法),
    # 免得每次都要靠翻整屏日志来判定。dsh 下不能用管道,重定向在本机也不可靠,
    # 落一个 JSON 文件是最稳的取证方式。
    try:
        import json as _json
        import tempfile as _tempfile
        _result = {
            'recon': recon,
            'escaped': escaped,
            'leaked': leaked,
            'state_checks': {k: bool(v) for k, v in checks.items()},
            'verdict': 'SANDBOX_BROKEN' if (escaped or leaked) else 'BLOCKED_ALL_ESCAPES',
        }
        with open(os.path.join(_tempfile.gettempdir(), 'escape2_result.json'),
                  'w', encoding='utf-8') as fp:
            _json.dump(_result, fp, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()

    if escaped or leaked:
        print('   => 沙盒被拆(确实取得了越权能力,bug 复现)')
        return 2
    print('   => 沙盒守住了(仅侦察成功 —— 内省能拿到对象,但拿不到任何越权能力)')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
