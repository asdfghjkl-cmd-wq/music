# -*- coding: utf-8 -*-
"""Phase 2 验证:用真实的 b.Plugin + 真实的 plugin/ 目录跑一遍装载流程(无 GUI)。"""

import io
import json
import logging
import os
import sys
import traceback

sys.path.insert(0, '.')
import b
import plugin_sandbox as ps

FAIL = []


def test(fn):
    try:
        fn()
    except Exception:
        FAIL.append(fn.__name__)
        print(f'FAIL {fn.__name__}')
        traceback.print_exc()
    else:
        print(f'ok   {fn.__name__}')
    return fn


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
            fn()                      # 立刻执行,等价于 mainloop 的第一轮
        return 'id'

    def destroy(self):
        pass


class FakeTk:
    def __init__(self, registry):
        self.menu = FakeMenu()
        self.app = FakeApp()
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = registry


def load_all():
    """照 b.py 装载器的顺序走一遍(目录排序、共享 assigned_names、init_env/init_i)。"""
    reg = b.env_box()
    host = FakeTk(reg)
    assigned = set()
    plugins = []
    for asa in sorted(os.listdir('plugin')):
        mn = os.path.join('plugin', asa)
        if not os.path.isdir(mn) or not os.path.isfile(os.path.join(mn, 'plugin.json')):
            continue
        n = b.Plugin(mn, reg, assigned)
        assigned.add(n.name)
        n.init_env()
        n.init_i(host)
        plugins.append(n)
    host.plugin_list = plugins
    reg.seal()
    return reg, host, plugins


@test
def t_wired():
    assert b.env_box is ps.env_box, 'b.py 还得用 plugin_sandbox 的 env_box'
    assert not hasattr(b, 'SandBox'), '不是重点,只是提醒'
    reg, host, plugins = load_all()
    names = sorted(p.name for p in plugins)
    # 这份清单跟着 plugin/ 目录走:plugin/ 里每个带 plugin.json 的目录都会装载。
    # 当前是 9 个(escape=P1 越权样本、escape2=P3"模块 globals"样本、
    # test-1 自报 sandbox_escape_2、x 自报 test、fs 自报 test_1、n 自报 debug)。
    # 用集合比较而不是写死顺序,新增插件时只需改这一处数字。
    assert len(names) == len(set(names)), f'插件显示名不该重名:{names}'
    assert set(names) == {'debug', 'exit_add', 'sandbox_demo', 'sandbox_escape',
                          'sandbox_escape2', 'sandbox_escape_2', 'test',
                          'test_1', 'test_2'}, names
    assert reg.is_sealed(), '装载结束必须封存'
    # env_id 现在由宿主按插件目录推导(不再采纳 plugin.json 自报的 env_id),
    # 且跨目录共用同一个 env_id 会被 create() 拒绝 —— 所以是"每个插件一个沙盒"。
    # 原来这里断言的是 6(旧语义下 test/test_1 共用一个)。
    assert len(reg.boxes()) == len(plugins), [x.env_id for x in reg.boxes()]


@test
def t_existing_plugins_still_work():
    reg, host, plugins = load_all()
    by = {p.name: p for p in plugins}
    # 两个有 GUI 动作的插件照常在 init 里注册菜单(按 label 找,不写死顺序:
    # plugin/escape 也会挂它自己的菜单项)
    labels = [c['label'] for c in host.menu.commands]
    assert 'debug' in labels and 'exit' in labels, labels
    debug_cmd = next(c['command'] for c in host.menu.commands if c['label'] == 'debug')
    assert callable(debug_cmd)
    # H2 之后 x / fs 各自声明了 env_id=1,但来自不同目录 —— 第二个会被
    # create() 拒绝并回退到自己的 identity 命名空间,而不是共用同一个沙盒。
    # (旧语义是"共用 env_id 即共用命名空间",那正是跨插件互相覆盖变量的来源)
    assert by['test']._box is not by['test_1']._box, \
        '不同目录的插件不该共用一个沙盒'
    assert by['test'].env_id != by['test_1'].env_id
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        by['test_1'].run(host)                 # command: print(a),exec_path: play
    finally:
        sys.stdout = old
    # a=64 仍由 x 的 init 写进它自己的命名空间;fs 跑的是另一个命名空间
    assert by['test']._env_dict.get('a') == 64, by['test']._env_dict.get('a')
    # 点 debug 菜单项(用户的真实崩溃点就在这里):askstring 打桩,别真弹窗
    import tkinter.simpledialog as sd
    orig_ask = sd.askstring
    sd.askstring = lambda *a, **k: '1+1'
    buf2, old2 = io.StringIO(), sys.stdout
    sys.stdout = buf2
    try:
        debug_cmd()                            # debug 插件的 debug()
    finally:
        sys.stdout = old2
        sd.askstring = orig_ask
    out2 = buf2.getvalue()
    assert '2' in out2 and '1+1' in out2, f'debug() 没跑通:{out2!r}'
    # can_exec=false 的插件一行代码都不许跑
    nb = by['test_2']
    assert nb.can_exec is False and nb.init_ok is False
    assert 'm' not in nb._env_dict, 'can_exec=false 的插件竟然执行了 init'


@test
def t_namespace_hardened():
    reg, host, plugins = load_all()
    for p in plugins:
        ns = p._env_dict
        assert ns['__env_id__'] == p.env_id
        assert 'subprocess' not in ns and 'vlc' not in ns
        assert ns['__builtins__']['__import__'] == p._box.import_module
        for probe in (lambda: ns['os'].sys,
                      lambda: ns['os'].environ,
                      lambda: ns['__builtins__']['__import__']('ctypes'),
                      lambda: ns['open']('C:\\Windows\\win.ini')):
            try:
                probe()
            except ps.SandboxDenied:
                pass
            else:
                raise AssertionError(f'{p.name}:沙盒漏了一条')


@test
def t_privilege_unsafe_fails_closed():
    reg, host, plugins = load_all()
    debug = {p.name: p for p in plugins}['debug']
    # plugin/n/plugin.json 声明了 no_sandbox_really,但没经用户确认前必须仍是受限的
    assert debug.sandbox_policy.unsafe is False
    assert any('不受沙盒限制' in w for w in debug.sandbox_policy.warnings)
    try:
        debug._env_dict['os'].system('echo pwned')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('声明 unsafe 的插件在未确认前竟然不受限')


@test
def t_escape_fixture_blocked_through_loader():
    reg = b.env_box()
    host = FakeTk(reg)
    n = b.Plugin(os.path.join('plugin', 'nb'), reg, set())
    n.init_env()
    n.can_exec = True                      # 假装用户打开了它
    n.init_ok = False
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        n.init_i(host)                     # 不能抛出去,也不能真的执行成功
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert '被拦截' in out, out
    assert reg.sandbox(n.env_id) is n._box
    assert reg.a.get(n.env_id) is not None

    # 直接试"给自己授权",必须被宿主凭据挡住
    try:
        exec(compile('pro.env_dict.grant(__env_id__,["no_sandbox_really"])', '<t>', 'exec'),
             n._env_dict)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('插件竟然成功给自己授权')
    # pro 门面把 env_dict 拒了(要么是"不能访问 pro.env_dict",要么在旧写法下
    # 走到沙盒的"给自己授权"检查)—— 两种都是拦下,只要留下审计记录即可
    assert any(e['action'].startswith('violation') for e in n._box.events), n._box.events[:3]


@test
def t_sealed_registry():
    reg, host, plugins = load_all()
    for call in (lambda: reg.grant('1', 'fs:write', 'C:\\'),
                 lambda: reg.grant('1', 'fs:write', 'C:\\', _host_token=ps._HOST_TOKEN)):
        try:
            call()
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError('封存后还能改策略')
    try:
        reg.get('nope')
    except KeyError:
        pass
    else:
        raise AssertionError('未知 env_id 应当报错')
    # 审计记录在案(env_id 现在等于插件目录 identity,不再是自报的 "2")
    n = {p.name: p for p in plugins}.get('test_2') or list(plugins)[0]
    box = reg.sandbox(n.env_id)
    assert box.events, '逃逸尝试应当留下审计记录'


if __name__ == '__main__':
    print('FAILED:', FAIL or 'none')
    raise SystemExit(1 if FAIL else 0)
