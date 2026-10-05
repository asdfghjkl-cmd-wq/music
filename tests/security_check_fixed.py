# -*- coding: utf-8 -*-
"""修订版安全验证用例(针对修复副本 `plugin_sandbox/plugin_sandboxa_fixed.py`)。

用法(项目根目录,**不用管道** —— dsh 下不能用管道):
    & .\\.venv\\Scripts\\python.exe .\\tests\\security_check_fixed.py

与 `tests/` 下原有套件的区别(即本文件修掉的 D4 / D5 / 伪帧三处问题):

* **D5 隔离** —— 原有套件共用 `_t_plugin*` / `_t_cfg` 等固定目录,在同一个进程里顺序
  跑会互相污染(`dsh/fake_filename_integration_test.py` 单跑 `t_wired` 通过,
  放进 `tests/run_all.py` 就 FAIL)。本文件用**带 pid 的唯一目录**,结束即删,
  不依赖也不影响任何其它套件。
* **D4 路径** —— 原有断言写的是根目录 `SANDBOX.md`,实际文件在 `plugin_sandbox/`。
  这里按真实位置断言。
* **伪帧** —— 原有 `run_tagged` 只用"文件名带 `<plugin 名字 ` 前缀"冒充插件帧,
  而帧身份早已改成 **code 对象身份**,于是测试里那段代码被判成宿主代码、钩子不管,
  导致 `t_audit_blocks_introspection` 等用例长期误报失败。本文件的 `run_as_plugin`
  按装载器的真实做法**登记 code 身份**;另有专门用例验证"**未登记**的伪造帧"
  现在也会被 **fail-closed** 拦下(M1)。
"""

import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import plugin_sandbox.plugin_sandboxa as ps

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FAIL = []
RAN = []
_SEQ = itertools.count()
# 唯一目录,避免与 tests/ 下其它套件(以及并发进程)互相踩
TMP = os.path.join(ROOT, '_seccheck_%d' % os.getpid())
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


class FakePro:
    def __init__(self):
        self.menu = FakeMenu()
        self.app = FakeApp()
        self.music_dict = {}
        self.plugin_list = []


def test(fn):
    """沿用 tests/ 原有风格:装饰时**立即执行**该用例,把结果记进 FAIL/RAN。

    注意:因此**不能**在 __main__ 里再调用一遍 —— 用例之间共享模块级的
    `_FRAME_TAGS` / `_CodeLedger.code_owner`(按 code 对象的值比较),
    跑第二遍会让新 compile 的 code 命中上一遍的盒子,出现假失败。
    """
    RAN.append(fn.__name__)
    try:
        fn()
    except Exception:
        FAIL.append(fn.__name__)
        print(f'FAIL {fn.__name__}')
        traceback.print_exc()
    else:
        print(f'ok   {fn.__name__}')
    return fn


def make_dir(name, sandbox=None):
    d = os.path.join(TMP, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    with io.open(os.path.join(d, 'plugin.json'), 'w', encoding='utf-8') as fp:
        json.dump({'name': name, 'can_exec': True, 'sandbox': sandbox or {}}, fp)
    return d


def make_box(name, sandbox=None, plugin_dir=None):
    # 每个用例用**唯一**的 env_id / name / 目录名:
    #   * _FRAME_TAGS 是按 `<plugin 名字 ` 前缀匹配的,同名 tag 会让上一个用例的
    #     盒子被认成本用例的盒子;
    #   * _CodeLedger.code_owner 是按 **code 对象的值**比较的(含 co_filename),
    #     同名 tag + 同样源码会让"本用例新 compile 的 code"命中的是上一个用例的盒子。
    #   这两条都会造成跨用例污染 —— 正是本文件要修掉的 D5 那类问题,自己更不能犯。
    uniq = '%s_%d' % (name, next(_SEQ))
    d = plugin_dir or make_dir(uniq)
    policy = ps.parse_policy(sandbox, None, d)
    return ps.SandBox(uniq, uniq, d, policy), d


def run_as_plugin(box, src):
    """按装载器的真实做法执行一段"插件代码":编译 + 登记 code 身份 + exec。"""
    code = compile(src, box.frame_tag + 'init>', 'exec')
    box.register_plugin_code(code)
    exec(code, box.namespace)


def run_unregistered_plugin(box, src):
    """执行一段**未登记**的代码,但文件名自称是该插件的帧(伪帧 / 动态 compile 场景)。"""
    code = compile(src, box.frame_tag + 'forged>', 'exec')
    exec(code, box.namespace)


# ---------------------------------------------------------------- H2 env_id

@test
def t_env_id_conflict_rejected():
    """H2:同一 env_id 但不同插件目录,必须拒绝共用沙盒(原先会复用并合并策略)。"""
    reg = ps.env_box()
    a = make_dir('env_a')
    b = make_dir('env_b')
    pa = ps.parse_policy({}, None, a)
    pb = ps.parse_policy({}, None, b)
    box = reg.create('shared', 'a', a, pa, _host_token=ps._HOST_TOKEN)
    assert box is not None
    try:
        reg.create('shared', 'b', b, pb, _host_token=ps._HOST_TOKEN)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('不同插件目录用同一 env_id 竟然复用了沙盒')
    # 同一目录重复创建应当仍可用(返回同一个 box)
    again = reg.create('shared', 'a', a, ps.parse_policy({}, None, a),
                       _host_token=ps._HOST_TOKEN)
    assert again is box, '同一插件目录重建应当返回同一个 box'


# ---------------------------------------------------------------- H4 modules

@test
def t_modules_need_host_approval():
    """H4:manifest 里的 sandbox.modules 不再自动放行,必须宿主明示批准。"""
    d = make_dir('mods')
    p = ps.parse_policy({'modules': ['ctypes']}, None, d)
    assert list(p.modules) == [], f'未批准就放行了模块:{p.modules}'
    assert 'ctypes' in tuple(p.modules_requested), '申请没有记进 modules_requested'

    box = ps.SandBox('mods', 'mods', d, p)
    # 插件帧/无令牌调用必须被拒
    for kwargs in ({}, {'_host_token': object()}):
        try:
            box.approve_modules(['ctypes'], **kwargs)
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError('approve_modules 竟然不需要宿主凭据')
    # 宿主批准后才真的放行
    box.approve_modules(['ctypes'], _host_token=ps._HOST_TOKEN)
    assert 'ctypes' in tuple(box.policy.modules), '宿主批准后仍未放行'
    # 已封存则不能再改
    box.seal()
    try:
        box.approve_modules(['shutil'], _host_token=ps._HOST_TOKEN)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('封存后竟然还能追加放行模块')


# ---------------------------------------------------------------- M1 伪帧 fail-closed

@test
def t_forged_frame_is_denied():
    """M1:co_filename 自称插件帧、但 code 对象未登记 → 必须按该插件判权(拦下),
    而不是原先的"判定按宿主处理"直接放行。"""
    ps.install_audit_hook()
    box, d = make_box('forged')
    box.attach_host(FakePro())
    box.namespace['real_open'] = io.open
    box.register_plugin_code(compile('', box.frame_tag + 'init>', 'exec'))
    try:
        run_unregistered_plugin(box, f"real_open(r'{OUTSIDE}')")
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError(f'未登记的伪造帧竟然读走了 {OUTSIDE}(fail-open 未修)')
    assert any(e['action'] == 'violation:fake_frame' for e in box.events), \
        '伪造帧没有留下审计'


@test
def t_registered_plugin_frame_still_works():
    """回归:正常登记的插件代码读自己的目录必须照旧可用(别修出功能退化)。"""
    ps.install_audit_hook()
    box, d = make_box('normal')
    box.attach_host(FakePro())
    inside = os.path.join(d, 'in.txt')
    with io.open(inside, 'w', encoding='utf-8') as fp:
        fp.write('hi')
    box.namespace['real_open'] = io.open
    run_as_plugin(box, f"assert real_open(r'{inside}').read() == 'hi'")
    # 目录外仍然要拦
    try:
        run_as_plugin(box, f"real_open(r'{OUTSIDE}')")
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('登记过的插件帧读目录外竟然放行')


# ---------------------------------------------------------------- M2 identity_ok

@test
def t_identity_ok_exposed():
    """M2:插件代码身份登记的结果必须能被宿主看到(登记失败不许静默继续)。"""
    box, d = make_box('identok')
    assert hasattr(box, 'identity_ok'), 'SandBox 没有暴露 identity_ok'
    assert box.identity_ok is True, '没有任何源码时 identity_ok 应为 True(无事可登记)'


# ---------------------------------------------------------------- M4 grant root

@test
def t_grant_root_does_not_ascend():
    """M4:授权根不许把"不存在的目标"上浮到父目录(否则一个文件的授权变成整盘)。"""
    box, d = make_box('grantroot')
    missing = os.path.join(TMP, 'not_created_yet_dir')
    got = box._grant_root(missing)
    assert os.path.normcase(os.path.abspath(got)) == os.path.normcase(os.path.abspath(missing)), \
        f'不存在的目标被上浮到了父目录:{missing} -> {got}'
    # 已存在的目录原样返回
    real_dir = make_dir('grantroot_real')
    assert os.path.normcase(os.path.abspath(box._grant_root(real_dir))) == \
        os.path.normcase(os.path.abspath(real_dir))
    # 已存在的文件取所属目录(这个行为是对的,保留)
    f = os.path.join(real_dir, 'x.txt')
    with io.open(f, 'w', encoding='utf-8') as fp:
        fp.write('x')
    assert os.path.normcase(os.path.abspath(box._grant_root(f))) == \
        os.path.normcase(os.path.abspath(real_dir))


# ---------------------------------------------------------------- L3 norm cache

@test
def t_norm_cache_invalidated_on_write():
    """L3:写操作之后要清 _norm 缓存,免得 realpath 结果陈旧。"""
    assert hasattr(ps, '_norm_cache_clear'), '缺少 _norm_cache_clear'
    # 必须真的给写权限,否则 check_fs 会先拒掉,根本走不到"写成功后清缓存"那一步
    box, d = make_box('normcache', {'fs_write': ['.']})
    box.namespace['real_os'] = __import__('os')
    # 先读一次把缓存填上
    _ = box.fs_listdir(d)
    ps._NORM_CACHE['__probe__'] = 'sentinel'
    target = os.path.join(d, 'made.txt')
    box.fs_write_call(io.open, '写入文件', target, 'w').close()
    assert '__probe__' not in ps._NORM_CACHE, '写操作后 _norm 缓存没有被清'


# ---------------------------------------------------------------- D3 死状态

@test
def t_dead_depth_state_removed():
    """D3:_FRAME_LOCAL.depth 是只写不读的死状态,修订版应已删除。"""
    assert not hasattr(ps, '_facade_enter'), '死函数 _facade_enter 仍在'
    assert not hasattr(ps, '_facade_exit'), '死函数 _facade_exit 仍在'
    assert not hasattr(ps._FRAME_LOCAL, 'depth'), '_FRAME_LOCAL.depth 死状态仍在'
    # _FRAME_LOCAL 本身要保留:钩子的异常分支还在用 .warned
    assert hasattr(ps, '_FRAME_LOCAL'), '_FRAME_LOCAL 不该被整个删掉(.warned 仍在用)'


# ---------------------------------------------------------------- D4 文档路径

@test
def t_docs_at_real_path():
    """D4:SANDBOX.md 的真实位置是 plugin_sandbox/,不是项目根目录。"""
    doc_path = os.path.join('plugin_sandbox', 'SANDBOX.md')
    assert os.path.isfile(doc_path), f'缺 {doc_path}'
    doc = io.open(doc_path, encoding='utf-8').read()
    for needle in ('不是安全边界', 'SandboxDenied'):
        assert needle in doc, f'SANDBOX.md 没提到 {needle}'


if __name__ == '__main__':
    # 用例已在 @test 装饰时全部执行完毕(见 test() 的 docstring),这里只做清理与汇总。
    # **不要**在这里再调一遍:模块级的帧注册表 / code 身份账本会让第二遍出现假失败。
    shutil.rmtree(TMP, ignore_errors=True)
    # 单独写一份机器可读结果(放系统临时目录,不污染仓库根目录):
    # 控制台的 stdout/stderr 经常交错,光看回显容易把"哪一条真的失败了"看错(本轮就踩过)。
    try:
        with io.open(os.path.join(tempfile.gettempdir(), 'seccheck_result.json'), 'w',
                     encoding='utf-8') as fp:
            json.dump({'total': len(RAN), 'ran': RAN, 'failed': FAIL,
                       'ok': not FAIL}, fp, ensure_ascii=False, indent=2)
    except Exception:
        traceback.print_exc()
    print()
    if FAIL:
        print(f'失败:{FAIL}')
        raise SystemExit(1)
    print('全部通过')
    raise SystemExit(0)
