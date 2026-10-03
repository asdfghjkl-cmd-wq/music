# -*- coding: utf-8 -*-
"""Phase 3 验证:授权弹窗、会话/持久化授权、unsafe 确认、装载期展示能力、注册表加固。"""

import io
import json
import os
import shutil
import sys
import types
import traceback

sys.path.insert(0, '.')
import ttkbootstrap

import b
import plugin_sandbox as ps

FAIL = []
CFG_DIR = '_t_cfg'
CFG = os.path.join(CFG_DIR, 'config.json')


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


def write_config(data):
    os.makedirs(CFG_DIR, exist_ok=True)
    with io.open(CFG, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False)


def read_config():
    c = b.Config()                 # 读真实的 config.json(只读)
    c.path = CFG
    c.load()
    return c


def FakeHost(config):
    """借用真实的 Tkapp 方法(_plugin_ask 等只用到 app 和 config),但不跑 GUI 初始化。"""
    h = b.Tkapp.__new__(b.Tkapp)
    h.app = None
    h.config = config
    return h


def patch_yesno(answer_holder):
    calls = []

    def fake(message, title=' ', **kw):
        calls.append({'message': message, 'title': title, 'buttons': kw.get('buttons')})
        return answer_holder[0]

    orig = ttkbootstrap.Messagebox.yesno
    ttkbootstrap.Messagebox.yesno = staticmethod(fake)
    return calls, lambda: setattr(ttkbootstrap.Messagebox, 'yesno', orig)


@test
def t_config_roundtrip():
    write_config({})
    c = read_config()
    assert c.plugin_grants == {} and c.plugin_unsafe == []
    target = os.path.join(CFG_DIR, 'data')
    os.makedirs(target, exist_ok=True)
    c.grant('demo', 'fs:write', target)
    c.grant('demo', 'fs:write', target)          # 重复不该记两遍
    c.grant('demo', 'net')
    c.plugin_unsafe.append('demo')
    c.save()

    c2 = read_config()
    got = c2.grants_for('demo')
    assert got.count(('fs:write', os.path.normpath(os.path.abspath(target)))) == 1, got
    assert ('net', None) in got, got
    assert ('fs:read', None) not in got
    assert c2.plugin_unsafe == ['demo']
    assert c2.grants_for('nobody') == []
    # 再存一次不该丢字段
    c2.save()
    c3 = read_config()
    assert c3.grants_for('demo') == got and c3.plugin_unsafe == ['demo']


@test
def t_config_invalid():
    write_config({
        'plugin_grants': {
            'ok': {'fs_read': 'D:\\x', 'fs_write': [1, 'D:\\y', ''], 'net': True, 'proc': 'yes'},
            'bad': 5,
            'nokey': {},
        },
        'plugin_unsafe': ['a', '', 5, 'a', None],
    })
    c = read_config()
    assert c.plugin_grants.get('ok') == {'fs_read': ['D:\\x'], 'fs_write': ['D:\\y'], 'net': True}, c.plugin_grants
    assert 'bad' not in c.plugin_grants and 'nokey' not in c.plugin_grants
    assert c.plugin_unsafe == ['a'], c.plugin_unsafe

    write_config({'plugin_grants': 'nope', 'plugin_unsafe': 'nope'})
    c = read_config()
    assert c.plugin_grants == {} and c.plugin_unsafe == []


@test
def t_ask_mapping():
    box = ps.SandBox('demo', 'demo', os.path.abspath('plugin/q'),
                     ps.parse_policy(None, None, os.path.abspath('plugin/q')))
    host = FakeHost(read_config())
    answer = [None]
    calls, restore = patch_yesno(answer)
    try:
        for label, want in (('允许本次', ps.ASK_YES),
                            ('本次运行都允许', ps.ASK_SESSION),
                            ('总是允许', ps.ASK_ALWAYS),
                            ('拒绝', 'no'),
                            (None, 'no')):
            answer[0] = label
            got = b.Tkapp._plugin_ask(host, box, 'fs:write', 'D:\\x\\a.txt', "open('w')")
            assert got == want, (label, got, want)
    finally:
        restore()
    msg = calls[0]['message']
    assert 'demo' in msg and 'D:\\x\\a.txt' in msg and "open('w')" in msg, msg
    assert calls[0]['buttons'][-1] == '拒绝', calls[0]['buttons']      # 默认按钮是"拒绝"
    assert len(calls[0]['buttons']) == 4


@test
def t_persist():
    write_config({})
    host = FakeHost(read_config())
    target = os.path.join(CFG_DIR, 'out')
    os.makedirs(target, exist_ok=True)
    b.Tkapp._plugin_persist(host, 'demo', 'fs:write', target)
    c = read_config()
    assert ('fs:write', os.path.normpath(os.path.abspath(target))) in c.grants_for('demo')
    b.Tkapp._plugin_persist(host, 'demo', 'net', None)
    assert ('net', None) in read_config().grants_for('demo')


@test
def t_grants_applied_at_load():
    """装载时把 config 里的授权喂回沙盒 —— 等价于 load_p 里的那段循环。"""
    write_config({})
    d = os.path.abspath(os.path.join('plugin', 'q'))
    reg = b.env_box()
    n = b.Plugin(d, reg, set())
    n.init_env()
    assert n._box.can('fs:write', os.path.join(CFG_DIR, 'x')) is False
    cfg = read_config()
    target = os.path.join(CFG_DIR, 'granted')
    os.makedirs(target, exist_ok=True)          # 白名单是目录级的,目标目录要真实存在
    cfg.grant(n.name, 'fs:write', target)
    for cap, tgt in cfg.grants_for(n.name):
        n._box.grant(cap, tgt, _host_token=ps._HOST_TOKEN)
    assert n._box.can('fs:write', os.path.join(target, 'a.txt')) is True
    reg.seal()
    assert n._box.can('fs:write', os.path.join(target, 'a.txt')) is True     # 封存不影响已授的
    assert n._box.can('fs:write', os.path.join(CFG_DIR, 'other')) is False


@test
def t_unsafe_needs_user():
    write_config({})
    d = os.path.abspath(os.path.join('plugin', 'n'))          # 声明了 no_sandbox_really
    reg = b.env_box()
    n = b.Plugin(d, reg, set())
    assert n.sandbox_policy.unsafe_requested is True
    assert n.sandbox_policy.unsafe is False, '没经用户确认就不该生效'
    assert any('不受沙盒限制' in w for w in n.sandbox_policy.warnings)
    n.init_env()

    # 插件自己解不开
    for call in (lambda: n._box.set_unsafe(True),
                 lambda: n._box.grant('net', None)):
        try:
            call()
        except ps.SandboxDenied:
            pass
        else:
            raise AssertionError('插件竟然能给自己解沙盒/加权限')
    assert any('解除自己的沙盒限制' in e['detail'] or '给自己授权' in e['detail']
               for e in n._box.events)

    host = FakeHost(read_config())
    answer = ['否']
    calls, restore = patch_yesno(answer)
    try:
        assert b.Tkapp._confirm_unsafe(host, n) is False
        assert n.sandbox_policy.unsafe is False
        assert read_config().plugin_unsafe == []
        assert '完全不受沙盒限制' in calls[0]['message']
        answer[0] = '是'
        assert b.Tkapp._confirm_unsafe(host, n) is True
    finally:
        restore()
    assert n.sandbox_policy.unsafe is True
    assert read_config().plugin_unsafe == ['debug'], read_config().plugin_unsafe
    try:
        n._box.can('net')
    except Exception:
        raise AssertionError('unsafe 之后 can() 不该抛')

    # 已确认过的插件:不再问,直接用
    n2 = b.Plugin(d, reg, set())
    n2.init_env()
    host2 = FakeHost(read_config())
    calls2, restore2 = patch_yesno(['否'])
    try:
        assert b.Tkapp._confirm_unsafe(host2, n2) is True
        assert calls2 == [], '确认过的插件不该再弹窗'
    finally:
        restore2()

    # 封存之后连宿主也不能改
    n2._box.seal()
    try:
        n2._box.set_unsafe(False, _host_token=ps._HOST_TOKEN)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('封存后宿主还能改 unsafe')


@test
def t_registry_hardened():
    reg = b.env_box()
    d = os.path.abspath(os.path.join('plugin', 'q'))
    n = b.Plugin(d, reg, set())
    n.init_env()
    crafted = ps.parse_policy({'fs_write': ['C:\\'], 'net': True}, None, d)

    # 插件(无凭据)不能改造已有沙盒
    before = (tuple(n._box.policy.fs_write), n._box.policy.net)
    same = reg.create(n.env_id, 'evil', d, crafted)
    assert same is n._box
    assert (tuple(n._box.policy.fs_write), n._box.policy.net) == before, '无凭据竟然改了策略'
    assert any('改造已有沙盒' in e['detail'] for e in n._box.events)

    # 无凭据也不能新建 env
    try:
        reg.create('brand-new', 'evil', d, crafted)
    except ps.SandboxDenied:
        pass
    else:
        raise AssertionError('插件竟然建出了新沙盒')

    # 宿主在封存后也只能把命名空间登记回来,不能改策略
    reg.seal()
    reg.create(n.env_id, 'evil', d, crafted, _host_token=ps._HOST_TOKEN)
    assert (tuple(n._box.policy.fs_write), n._box.policy.net) == before, '封存后策略被改了'
    assert reg.get(n.env_id) is n._box.namespace


@test
def t_loader_shows_capabilities():
    """装载确认框要带上插件申请的能力(源码级确认,顺便验证 describe 的输出)。"""
    src = io.open('b.py', encoding='utf-8').read()
    assert '它申请的权限' in src and 'n.sandbox_policy.describe()' in src
    policy = ps.parse_policy({'fs_read': ['.'], 'fs_write': ['data'], 'net': True},
                             None, os.path.abspath('plugin/q'))
    text = policy.describe()
    assert '写入' in text and 'data' in text and '网络:允许' in text, text
    unsafe_policy = ps.parse_policy(None, ['no_sandbox_really'], os.path.abspath('plugin/q'))
    assert '不受沙盒限制' in unsafe_policy.describe()
    assert '未确认' in unsafe_policy.describe() or '你没确认' in unsafe_policy.describe()


if __name__ == '__main__':
    shutil.rmtree(CFG_DIR, ignore_errors=True)
    print('FAILED:', FAIL or 'none')
    raise SystemExit(1 if FAIL else 0)
