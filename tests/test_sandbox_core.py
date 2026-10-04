# -*- coding: utf-8 -*-
"""Phase 1 无头验证:plugin_sandbox 的能力判定、封存、门面与命名空间。"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback

sys.path.insert(0, '.')
import plugin_sandbox as ps

ROOT = os.path.abspath('_t_plugin')
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


def make_plugin(name='demo', files=None, sandbox=None, privilege=None):
    d = os.path.join(ROOT, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    cfg = {'name': name, 'can_exec': True}
    if sandbox is not None:
        cfg['sandbox'] = sandbox
    if privilege is not None:
        cfg['privilege'] = privilege
    with io.open(os.path.join(d, 'plugin.json'), 'w', encoding='utf-8') as fp:
        json.dump(cfg, fp)
    for fn_name, text in (files or {}).items():
        with io.open(os.path.join(d, fn_name), 'w', encoding='utf-8') as fp:
            fp.write(text)
    return d


def make_box(name='demo', sandbox=None, privilege=None, files=None):
    d = make_plugin(name, files, sandbox, privilege)
    policy = ps.parse_policy(sandbox, privilege, d)
    box = ps.SandBox(name, name, d, policy)
    return box, d


class FakeMenu:
    def __init__(self):
        self.commands = []

    def add_command(self, **kw):
        self.commands.append(kw)


class FakeApp:
    def after(self, ms, fn=None):
        if fn is not None:
            fn()
        return 'id'

    def destroy(self):
        pass


class FakePro:
    def __init__(self, env_dict=None):
        self.menu = FakeMenu()
        self.app = FakeApp()
        self.plugin_list = []
        self.music_dict = {}
        self.env_dict = env_dict


# ---------------------------------------------------------------- 路径

@test
def t_paths():
    d = make_plugin('paths')
    assert ps.under(d, d)
    assert ps.under(d, os.path.join(d, 'a', 'b.txt'))
    assert ps.under(d, os.path.join(d, '..', 'paths', 'x'))       # .. 归一化后仍在里面
    assert not ps.under(d, os.path.join(d, '..'))
    assert not ps.under(d, os.path.join(d, '..', 'paths2'))
    assert not ps.under(d, os.path.join(d, '..', '..', 'Windows'))
    assert not ps.under(d, 'C:\\Windows\\System32' if os.name == 'nt' else '/etc/passwd')
    assert not ps.under(d, None)
    assert not ps.under(d, 42)
    assert ps.under(d, os.path.join(d, 'sub', 'not-yet-created.txt'))   # 不存在的路径也能判
    if hasattr(os, 'symlink'):
        link = os.path.join(d, 'link')
        try:
            os.symlink(os.path.dirname(d), link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass                                                     # 没权限就算了
        else:
            assert not ps.under(d, os.path.join(link, 'outside')), '符号链接不得逃出根目录'


# ---------------------------------------------------------------- 策略

@test
def t_policy():
    p = ps.parse_policy(None, None, ROOT)
    assert p.fs_read == [os.path.abspath(ROOT)] and p.fs_write == []
    assert (p.net, p.proc, p.ask, p.unsafe) == (False, False, True, False)
    assert '需要时向你申请' in p.describe()

    d = make_plugin('pol')
    p = ps.parse_policy({'fs_read': '..', 'fs_write': ['data'], 'net': True,
                         'proc': 1, 'ask': False, 'modules': ['numpy', 'bad name']},
                        None, d)
    assert p.fs_read == [os.path.abspath(os.path.join(d, '..'))]
    assert p.fs_write == [os.path.abspath(os.path.join(d, 'data'))]
    assert p.net is True and p.proc is False and p.ask is False
    assert p.modules == ['numpy']
    assert any('proc' in w for w in p.warnings) and any('bad name' in w for w in p.warnings)

    p = ps.parse_policy('not-a-dict', ['no_sandbox_really', 'built', 'nope'], d)
    assert p.unsafe_requested is True and p.unsafe is False and p.fs_write == []
    assert any('no_sandbox_really' not in w and 'privilege' in w for w in p.warnings)

    p2 = ps.parse_policy({'fs_write': ['x']}, None, d)
    p2.freeze()
    assert isinstance(p2.fs_read, tuple) and isinstance(p2.fs_write, tuple)
    try:
        p2.fs_write.append('C:\\')
    except AttributeError:
        pass
    else:
        raise AssertionError('freeze 之后策略仍可被就地修改')


# ---------------------------------------------------------------- 封存与授权

@test
def t_seal_grant():
    box, d = make_box('seal')
    box.grant('fs:write', os.path.join(d, 'data'), _host_token=ps._HOST_TOKEN)
    assert box.can('fs:write', os.path.join(d, 'data', 'x.txt'))

    # 插件式的自我授权:没有凭据,必须被拒
    for args in ((box.env_id, ['no_sandbox_really']), ('fs:write', 'C:\\')):
        try:
            box.grant(*args)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'插件自我授权竟然成功了:{args}')
    assert any('给自己授权' in e['detail'] for e in box.events)

    # 就算把 __sealed 掰成 False,没有凭据依然授不了权
    box._SandBox__sealed = False
    try:
        box.grant('fs:write', 'C:\\')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('掰掉 __sealed 之后竟然能自我授权')

    box.seal()
    assert box.is_sealed()
    try:
        box.grant('fs:write', os.path.join(d, 'data'), _host_token=ps._HOST_TOKEN)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('封存之后宿主仍然能改策略')


# ---------------------------------------------------------------- 命名空间

@test
def t_namespace():
    box, d = make_box('ns')
    ns = box.namespace
    assert ns['plugin_dir'] == d
    assert set(('json', 'os', 'sys', 'io', 'tkinter', 'ttkbootstrap', 'Image',
                'ImageTk', 'mutagen', 'open', '__sandbox__')) <= set(ns)
    b = ns['__builtins__']
    assert callable(b['print']) and b['open'] == box.fs_open
    assert 'eval' in b and 'exec' in b
    assert b['__import__'] == box.import_module
    for bad in ('__loader__', 'exit', 'quit', 'breakpoint', 'help'):
        assert bad not in b, f'内置 {bad} 不该出现在受控 builtins 里'
    assert ns['__sandbox__'].can('fs:read', d) is True
    assert '读取' in ns['__sandbox__'].describe()


# ---------------------------------------------------------------- 文件

@test
def t_fs_guarded():
    box, d = make_box('fs')
    with io.open(os.path.join(d, 'in.txt'), 'w', encoding='utf-8') as fp:
        fp.write('hi')

    assert box.fs_open(os.path.join(d, 'in.txt')).read() == 'hi'
    assert sorted(box.fs_listdir(d)) == ['in.txt', 'plugin.json']
    assert box.os_module().path.join('a', 'b') == os.path.join('a', 'b')
    assert box.os_module().path.exists(os.path.join(d, 'in.txt')) is True
    assert box.os_module().path.exists('C:\\Windows') is False        # 探测静默返回 False

    for target in (os.path.join(d, '..', 'outside.txt'),
                   os.path.join(d, 'sub', '..', '..', 'outside.txt'),
                   'C:\\Windows\\win.ini'):
        try:
            box.fs_open(target)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'越界读取竟然成功:{target}')

    for name, call in (
            ('写', lambda: box.fs_open(os.path.join(d, 'out.txt'), 'w')),
            ('删除', lambda: box.os_module().remove(os.path.join(d, 'in.txt'))),
            ('改名', lambda: box.os_module().rename(os.path.join(d, 'in.txt'),
                                                    os.path.join(d, 'in2.txt'))),
            ('建目录', lambda: box.os_module().makedirs(os.path.join(d, 'sub'))),
            ('environ', lambda: box.os_module().environ),
            ('chmod', lambda: box.os_module().chmod(os.path.join(d, 'in.txt'), 0o777)),
            ('fdopen', lambda: box.fs_open(1)),
    ):
        try:
            call()
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'{name} 竟然被允许了')
    assert os.path.isfile(os.path.join(d, 'in.txt')), '被拒的操作不该真的发生'


@test
def t_fs_write_granted():
    box, d = make_box('fsw', {'fs_write': ['out'], 'ask': False})
    target = os.path.join(d, 'out', 'a.txt')
    os.makedirs(os.path.join(d, 'out'), exist_ok=True)
    box.fs_open(target, 'w').write('x')
    assert io.open(target, encoding='utf-8').read() == 'x'
    assert box.can('fs:read', target)                       # 写权限隐含读


@test
def t_fs_ask():
    box, d = make_box('ask')
    calls = []

    def asker(sb, cap, target, detail):
        calls.append((cap, target))
        return answers.pop(0)
    answers = ['no']
    box.set_ask(asker)
    try:
        box.fs_open(os.path.join(d, 'a.txt'), 'w')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('用户说不该还放行')
    assert len(calls) == 1

    # 同一个目标不会反复弹窗
    try:
        box.fs_open(os.path.join(d, 'a.txt'), 'w')
    except ps.SandboxDenied:
        pass
    assert len(calls) == 1, '同一目标重复询问'

    # 会话授权:这次放行,并且目录进了会话白名单
    answers = ['session']
    box.fs_open(os.path.join(d, 'b.txt'), 'w').write('1')
    assert box.can('fs:write', os.path.join(d, 'c.txt'))
    assert len(calls) == 2

    # always:调用持久化回调
    seen = []
    box2, d2 = make_box('ask2')
    box2.set_ask(lambda *a: 'always')
    box2.set_persist(lambda name, cap, target: seen.append((name, cap, target)))
    box2.fs_open(os.path.join(d2, 'y.txt'), 'w')
    assert seen and seen[0][0] == 'ask2' and seen[0][1] == 'fs:write'

    # 非主线程不弹窗:直接拒绝
    box3, d3 = make_box('ask3')
    box3.set_ask(lambda *a: 'yes')
    result = {}

    def worker():
        try:
            box3.fs_open(os.path.join(d3, 't.txt'), 'w')
        except ps.SandboxDenied:
            result['denied'] = True
        else:
            result['denied'] = False
    th = threading.Thread(target=worker)
    th.start()
    th.join()
    assert result.get('denied') is True, '非主线程不该弹窗授权'
    assert any(e['action'] == 'ask_skipped' for e in box3.events)


# ---------------------------------------------------------------- 网络与进程

@test
def t_net_proc():
    box, d = make_box('net')
    for name, call in (
            ('socket', lambda: box.socket_module().socket()),
            ('create_connection', lambda: box.socket_module().create_connection(('127.0.0.1', 9), 0.1)),
            ('getaddrinfo', lambda: box.socket_module().getaddrinfo('localhost', 80)),
            ('urlopen', lambda: box.urllib_module().request.urlopen('http://127.0.0.1/')),
            ('HTTPConnection', lambda: box.http_module().client.HTTPConnection('127.0.0.1')),
            ('os.system', lambda: box.os_module().system('echo hi')),
            ('os.popen', lambda: box.os_module().popen('echo hi')),
            ('subprocess.run', lambda: box.subprocess_module().run([sys.executable, '-c', 'pass'])),
            ('subprocess.Popen', lambda: box.subprocess_module().Popen([sys.executable, '-c', 'pass'])),
    ):
        try:
            call()
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'{name} 竟然被允许了')

    ok, d2 = make_box('netok', {'net': True, 'proc': True, 'ask': False})
    s = ok.socket_module().socket()
    s.close()
    assert ok.urllib_module().parse.urlparse('http://x/y').netloc == 'x'
    # 授权后应当真的走到 subprocess:用一个不存在的程序证明"检查放行了、真实调用发生了"
    # (沙箱下不允许给子进程开管道,所以这里不用 capture_output)
    try:
        ok.subprocess_module().run(['dsh-no-such-program-xyz'])
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('授权后的 subprocess.run 没有走到真实实现')


# ---------------------------------------------------------------- import

@test
def t_import():
    box, d = make_box('imp')
    assert box.import_module('json') is json
    assert box.import_module('os') is box.module_builders()['os']
    for name in ('ctypes', 'shutil', 'importlib', 'pickle', 'vlc', 'b', 'numpy'):
        try:
            box.import_module(name)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'import {name} 竟然被放行')
    # 相对 import 不支持
    try:
        box.import_module('json', level=1)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('相对 import 竟然被放行')

    # 声明放行的模块不受门面代理(装载时会打印警告)
    ok, d2 = make_box('imp2', {'modules': ['xml']})
    assert ok.import_module('xml') is sys.modules['xml']


@test
def t_import_dotted():
    """__import__ 的契约:没有 fromlist 的 "import a.b" 必须返回顶层包 a。

    这条是回归测试:以前直接返回子模块,于是 `import tkinter.simpledialog`
    会把 tkinter 绑成子模块,之后 tkinter.simpledialog.xxx 全部炸掉
    (真实插件就是这么报的 AttributeError)。
    """
    import tkinter
    import tkinter.simpledialog as sd
    box, d = make_box('impdot')

    assert box.import_module('tkinter.simpledialog') is tkinter, '必须返回顶层包'
    assert box.import_module('tkinter.simpledialog', fromlist=('askstring',)) is sd
    assert box.import_module('os.path') is box.module_builders()['os']
    assert hasattr(box.module_builders()['os'], 'path')

    ns = box.namespace
    tag = box.frame_tag + 'init>'
    # 插件里最常见的写法:import a.b 之后用 a.b.x
    exec(compile('import tkinter.simpledialog\n'
                 'assert tkinter is not None\n'
                 '_cls = tkinter.simpledialog.SimpleDialog\n'
                 '_fn = tkinter.simpledialog.askstring\n',
                 tag, 'exec'), ns)
    assert ns['tkinter'] is tkinter, 'import tkinter.simpledialog 之后 tkinter 必须还是包'
    assert ns['_cls'] is sd.SimpleDialog and ns['_fn'] is sd.askstring

    # from X import Y(Y 是子模块)也要能拿到
    exec(compile('from tkinter import simpledialog as _sub\n'
                 'from tkinter import ttk as _ttk\n',
                 tag, 'exec'), ns)
    assert ns['_sub'] is sd
    assert ns['_ttk'] is tkinter.ttk

    # 没在白名单里的包,子模块照样拒
    for name in ('os.environ.x', 'shutil.os', 'ctypes.util'):
        try:
            box.import_module(name)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'import {name} 竟然被放行')


@test
def t_local_module():
    d = make_plugin('loc', {'helper.py': 'import os\nVALUE = 1\ntry:\n'
                                        '    os.system("echo pwned")\n'
                                        '    ESCAPED = True\n'
                                        'except Exception:\n'
                                        '    ESCAPED = False\n'})
    box = ps.SandBox('loc', 'loc', d, ps.parse_policy(None, None, d))
    mod = box.import_module('helper')
    assert mod.VALUE == 1
    assert mod.ESCAPED is False, '插件目录里的模块竟然拿到了真 os'
    assert box.import_module('helper') is mod
    try:
        box.import_module('nope_missing')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('未声明的模块竟然被放行')


# ---------------------------------------------------------------- 真实插件夹具

@test
def t_plugin_exit_add():
    d = os.path.abspath(os.path.join('plugin', 'q'))
    box = ps.SandBox('exit_add', 'exit_add', d, ps.parse_policy(None, None, d))
    pro = FakePro()
    box.attach_host(pro)
    code = io.open(os.path.join(d, 'a.py'), encoding='utf-8').read().replace('from b import *', '')
    exec(compile(code, 'q/a.py', 'exec'), box.namespace)
    assert pro.menu.commands and pro.menu.commands[0]['label'] == 'exit'


@test
def t_plugin_debug():
    d = os.path.abspath(os.path.join('plugin', 'n'))
    box = ps.SandBox('debug', 'debug', d, ps.parse_policy(None, None, d))
    pro = FakePro()
    box.attach_host(pro)
    code = io.open(os.path.join(d, 'a.py'), encoding='utf-8').read()
    code = code.replace('from b import * #app', '').replace('from b import *', '')
    exec(compile(code, 'n/a.py', 'exec'), box.namespace)
    assert pro.menu.commands and pro.menu.commands[0]['label'] == 'debug'
    ev = box.namespace['__builtins__']['eval']
    assert ev('1+1') == 2                       # 插件里的 eval 仍然可用(但被沙盒作用域包住)
    try:
        ev('os.system("echo pwned")')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('eval 里竟然能起进程')
    try:
        ev('__import__("ctypes")')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('eval 里竟然能 import ctypes')


@test
def t_escape_fixture():
    """plugin/nb/a.py 就是一份针对旧设计的越权样本,这里逐条确认它撞墙。"""
    d = os.path.abspath(os.path.join('plugin', 'nb'))
    registry = ps.env_box()
    policy = ps.parse_policy(None, None, d)
    box = registry.create(2, 'test_2', d, policy, _host_token=ps._HOST_TOKEN)
    registry.create(1, 'test', os.path.abspath(os.path.join('plugin', 'x')),
                    ps.parse_policy(None, None, os.path.abspath(os.path.join('plugin', 'x'))),
                    _host_token=ps._HOST_TOKEN)
    pro = FakePro(registry)
    pro.plugin_list = [type('P', (), {'init_env': lambda self: None})()]
    box.attach_host(pro)
    code = io.open(os.path.join(d, 'a.py'), encoding='utf-8').read().replace('from b import *', '')
    try:
        exec(compile(code, 'nb/a.py', 'exec'), box.namespace)
    except ps.SandboxDenied as e:
        print('     逃逸样本被拦下:', str(e)[:70])
    except Exception as e:
        raise AssertionError(f'逃逸样本以意外方式失败:{type(e).__name__}: {e}')
    else:
        raise AssertionError('逃逸样本竟然跑完了(os.sys.exit 之类的都没被拦?)')
    assert any('给自己授权' in e['detail'] or 'import' in e['detail'] or
               '能力' in e['detail'] or '禁用' in e['detail'] or '白名单' in e['detail']
               for e in box.events), box.events
    # 注册表被删掉的命名空间应当能自我修复,且宿主手里的对象不受影响
    assert registry.a.get(1) is not None
    del registry.a[1]
    assert registry.get(1)['plugin_dir'].endswith('x')


# ---------------------------------------------------------------- 注册表

@test
def t_registry():
    reg = ps.env_box()
    d1 = os.path.abspath(os.path.join('plugin', 'x'))
    d2 = os.path.abspath(os.path.join('plugin', 'fs'))
    b1 = reg.create(1, 'test', d1, ps.parse_policy(None, None, d1),
                    _host_token=ps._HOST_TOKEN)
    b1.attach_host(FakePro())
    exec(compile('a = 64', '<x/init>', 'exec'), b1.namespace)
    b2 = reg.create(1, 'test_1', d2, ps.parse_policy({'fs_read': ['..']}, None, d2),
                    _host_token=ps._HOST_TOKEN)
    assert b1 is b2, '共用 env_id 的插件应当是同一个沙盒'
    assert b2.namespace['a'] == 64, '共用命名空间应当能看到对方写的变量'
    assert str(d2) in ' '.join(b2.policy.fs_read) or str(d1) in ' '.join(b2.policy.fs_read)
    assert b2.namespace['pro'] is not None, '合并策略重建命名空间后不该丢掉宿主'

    try:
        reg.get(99)
    except KeyError:
        pass
    else:
        raise AssertionError('未知 env_id 应当报错')

    for name in ('fs:write',):
        try:
            reg.grant(1, name, 'C:\\')
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError('插件式的 registry.grant 竟然成功')
    reg.grant(1, 'fs:write', os.path.join(d2, 'data'), _host_token=ps._HOST_TOKEN)
    assert b1.can('fs:write', os.path.join(d2, 'data', 'f'))

    n = reg.seal()
    assert n == 1 and reg.is_sealed() and b1.is_sealed()


@test
def t_audit_logged():
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    h = H()
    logging.getLogger().addHandler(h)
    try:
        box, d = make_box('audit')
        try:
            box.fs_open('C:\\Windows\\win.ini')
        except ps.SandboxDenied:
            pass
        try:
            box.import_module('ctypes')
        except ps.SandboxDenied:
            pass
    finally:
        logging.getLogger().removeHandler(h)
    assert any('拦截' in m for m in records), records
    assert box.audit() and box.audit()[-1]['plugin'] == 'audit'


if __name__ == '__main__':
    shutil.rmtree(ROOT, ignore_errors=True)
    print('FAILED:', FAIL or 'none')
    raise SystemExit(1 if FAIL else 0)
