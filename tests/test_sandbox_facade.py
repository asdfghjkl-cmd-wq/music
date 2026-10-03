# -*- coding: utf-8 -*-
"""Phase 4 验证:pro 门面白名单、审计钩子兜底、文档与示例插件。"""

import io
import json
import os
import shutil
import sys
import traceback

sys.path.insert(0, '.')
import b
import plugin_sandbox as ps

FAIL = []
ROOT = os.path.abspath('_t_plugin4')
B_PY = os.path.abspath('b.py')
B_PY_LIT = B_PY.replace('\\', '\\\\')


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

    def delete(self, *a):
        raise AssertionError('不该被调用')


class FakeApp:
    def __init__(self):
        self.titles = []

    def after(self, ms, fn=None):
        if fn is not None:
            fn()
        return 'id'

    def title(self, text=None):
        self.titles.append(text)

    def after_cancel(self, i=None):
        pass

    def destroy(self):
        pass


class FakePro:
    def __init__(self):
        self.menu = FakeMenu()
        self.app = FakeApp()
        self.music_dict = {'a': {'music': 'orig'}}
        self.plugin_list = [type('P', (), {'name': 'a'})(), type('P', (), {'name': 'b'})()]


def make_box(name, sandbox=None):
    d = os.path.join(ROOT, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, 'plugin.json'), 'w', encoding='utf-8') as fp:
        json.dump({'name': name, 'can_exec': True, 'sandbox': sandbox or {}}, fp)
    policy = ps.parse_policy(sandbox, None, d)
    return ps.SandBox(name, name, d, policy), d


def run_tagged(box, src):
    """用"插件帧"执行一段代码(文件名带 <plugin 名字 前缀,审计钩子才认得)。"""
    code = compile(src, box.frame_tag + 'init>', 'exec')
    exec(code, box.namespace)


@test
def t_host_facade_whitelist():
    box, d = make_box('facade')
    pro = FakePro()
    facade = box.attach_host(pro)
    assert box.namespace['pro'] is facade

    facade.menu.add_command(label='hi', command=lambda: None)
    assert pro.menu.commands[-1]['label'] == 'hi'
    facade.menu.add_separator()
    facade.app.title('sandbox_demo')
    assert pro.app.titles == ['sandbox_demo']
    facade.app.after(0, lambda: None)
    assert callable(facade.app.destroy)
    assert callable(facade.app.after_cancel)

    snap = facade.music_dict
    snap['a']['music'] = 'CHANGED'
    assert pro.music_dict['a']['music'] == 'orig', '快照被改了会影响播放器'
    assert facade.plugin_names == ['a', 'b']

    for bad in ('tk', 'winfo_children', 'nametowidget', 'call', 'eval', 'tk_bisque'):
        try:
            getattr(facade.app, bad)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'pro.app.{bad} 竟然放行了')
    for bad in ('player', 'env_dict', 'plugin_list', 'config', 'sandbox_policy',
                'music_player', 'down_frame_listbox', 'app_tk'):
        try:
            getattr(facade, bad)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'pro.{bad} 竟然放行了')
    try:
        facade.menu.delete(0, 'end')
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('pro.menu.delete 竟然放行了')
    assert any('pro.app.tk' in (e['detail'] or '') for e in box.events)
    assert any('pro.menu.delete' in (e['detail'] or '') for e in box.events)


@test
def t_facade_delegate_has_no_widget():
    box, d = make_box('delegate')
    facade = box.attach_host(FakePro())
    after = facade.app.after
    assert not hasattr(after, '__self__'), '别把绑定方法交出去(它的 __self__ 是控件)'
    assert callable(after)
    # 闭包这条路仍然摸得到目标,这是文档里写明的上限,这里只确认"不是直接给控件"
    assert not hasattr(facade.menu.add_command, '__self__')


@test
def t_audit_blocks_introspection():
    ps.install_audit_hook()                    # 幂等
    box, d = make_box('audit')
    box.attach_host(FakePro())
    ns = box.namespace
    # 模拟插件用内省拿到"真的"东西(宿主直接塞进来,等于绕开了门面)
    import socket as real_socket
    import subprocess as real_subprocess
    ns['real_open'] = io.open
    ns['real_os'] = os
    ns['real_socket'] = real_socket
    ns['real_subprocess'] = real_subprocess
    targets = [
        "real_open(r'C:\\Windows\\win.ini')",
        "real_os.listdir(r'C:\\Windows')",
        "real_os.remove(r'C:\\Windows\\win.ini')",
        "real_socket.socket()",
        "real_socket.getaddrinfo('localhost',80)",
        "real_subprocess.Popen(['cmd','/c','echo','hi'])",
        "real_os.system('echo hi')",
    ]
    for src in targets:
        try:
            run_tagged(box, src)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError(f'审计钩子没拦住:{src}')
    # 已知缺口:os.stat/os.lstat 不产生审计事件,内省路径下"探测文件是否存在"挡不住。
    # 这里把它写成断言,免得以后有人以为漏了。
    run_tagged(box, "real_os.stat(r'C:\\Windows\\win.ini')")
    assert any(e['action'] == 'violation:audit' for e in box.events), box.events[:5]


@test
def t_audit_allows_own_dir_and_facade():
    box, d = make_box('audit2')
    box.attach_host(FakePro())
    box.namespace['real_open'] = io.open
    inside = os.path.join(d, 'in.txt')
    with io.open(inside, 'w', encoding='utf-8') as fp:
        fp.write('hi')
    run_tagged(box, f"assert real_open(r'{inside}').read() == 'hi'")   # 自己目录:放行

    outside = os.path.join(d, '..', 'outside.txt')
    try:
        run_tagged(box, f"real_open(r'{outside}')")
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('目录外的真实 open 竟然放行')

    # 门面里"只允许本次"的一次性授权,不能被钩子二次拦截
    box.set_ask(lambda *a: ps.ASK_YES)
    target = os.path.join(d, 'w.txt')
    try:
        run_tagged(box, f"open(r'{target}','w').write('ok')")
    except Exception as e:
        raise AssertionError(f'门面授权后仍被拦:{type(e).__name__}: {e}')
    assert io.open(target, encoding='utf-8').read() == 'ok'
    # 一次性授权不该变成长期授权
    try:
        run_tagged(box, f"real_open(r'{target}','w')")
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('一次性授权竟然对绕过门面的访问也生效')


@test
def t_audit_respects_host_and_unsafe():
    # 宿主(栈上没有插件帧)照常
    assert io.open(B_PY, encoding='utf-8').read(10)
    os.stat(B_PY)
    os.listdir('.')

    box, d = make_box('unsafe4')
    box.attach_host(FakePro())
    box.set_unsafe(True, _host_token=ps._HOST_TOKEN)
    box.namespace['real_open'] = io.open
    run_tagged(box, f"real_open(r'{B_PY_LIT}','rb').read(1)")   # unsafe:钩子不再管


@test
def t_hook_install_once_and_wired():
    assert ps.install_audit_hook() is False, '钩子不该装第二遍'
    src = io.open('b.py', encoding='utf-8').read()
    assert 'install_audit_hook' in src and 'if install_audit_hook():' in src


@test
def t_docs_and_example_plugin():
    assert os.path.isfile('SANDBOX.md'), '缺 SANDBOX.md'
    doc = io.open('SANDBOX.md', encoding='utf-8').read()
    for needle in ('不是安全边界', 'plugin_grants', 'plugin_unsafe', 'pro.menu',
                   'SandboxDenied', 'tests/run_all.py', 'Tcl'):
        assert needle in doc, f'SANDBOX.md 没提到 {needle}'

    ex = os.path.join('plugin_example', 'sandbox_demo')
    with io.open(os.path.join(ex, 'plugin.json'), encoding='utf-8') as fp:
        cfg = json.load(fp)
    assert cfg['sandbox']['fs_write'] == ['.'] and cfg['sandbox']['net'] is False
    assert cfg['sandbox']['fs_read'] == ['.'], '示例的越界演示要真的越界'

    # 示例插件能在沙盒里跑起来:注册三个菜单项 + 读自己的 manifest
    d = os.path.abspath(ex)
    policy = ps.parse_policy(cfg['sandbox'], None, d)
    box = ps.SandBox('sandbox_demo', 'sandbox_demo', d, policy)
    pro = FakePro()
    box.attach_host(pro)
    box.set_ask(lambda *a: 'no')                      # 运行期申请一律拒
    src = io.open(os.path.join(d, 'a.py'), encoding='utf-8').read()
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        exec(compile(src, box.frame_tag + 'init>', 'exec'), box.namespace)
        labels = [c.get('label') for c in pro.menu.commands]
        assert labels == ['sandbox_demo:说明', 'sandbox_demo:写个文件', 'sandbox_demo:越界试试'], labels
        # 写自己目录:策略里 fs_write=["."] → 直接成功
        assert box.namespace['write_demo_file']() is True
        # 越界读:被拒
        assert box.namespace['try_outside']() is None
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert '装载,manifest name = sandbox_demo' in out, out
    assert '预期行为' in out, out
    assert os.path.isfile(os.path.join(d, 'demo_out.txt'))
    os.remove(os.path.join(d, 'demo_out.txt'))        # 自测不留垃圾


if __name__ == '__main__':
    shutil.rmtree(ROOT, ignore_errors=True)
    print('FAILED:', FAIL or 'none')
    raise SystemExit(1 if FAIL else 0)
