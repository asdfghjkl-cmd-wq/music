r"""plugin/report 的真沙盒集成测试。

跟 dsh/report_gui_smoke.py 的区别:那份用假 __sandbox__ 只验界面逻辑;这份把插件
装进 plugin_sandbox 真沙盒里跑 —— 判权用的是 plugin.json 里声明的那份策略,
import 走的是沙盒的 import_local,_step 回调也真的由 pro.app.after 调度,
所以顺带验了"插件代码当 after 回调时仍然被认作插件帧"这条(第九批加固的主题)。

验的事:
  * 声明的 fs_read/fs_write 边界对不对(music/ 能读、b.py 不能写、C:\Windows 不能读);
  * a.py 在沙盒里能装载(所有 import 和 ttkbootstrap 组件都拿得到);
  * 菜单项挂得上,扫描跑得完,表格行数和曲库一致;
  * 全程审计日志里没有一条 violation —— 插件没触发任何沙盒拦截。

跑法(项目根目录):
    .\.venv\Scripts\python.exe dsh\report_sandbox_test.py
退出码 0 = 通过。
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(BASE, 'plugin', 'report')
if BASE not in sys.path:
    sys.path.insert(0, BASE)

FAILED = []


def check(ok, title, detail=''):
    if not ok:
        FAILED.append(title)
    print(f'[{"PASS" if ok else "FAIL"}] {title}' + (f'  -- {detail}' if detail else ''))


def load_music():
    entries = {}
    music_dir = os.path.join(BASE, 'music')
    for folder in sorted(os.listdir(music_dir)):
        path = os.path.join(music_dir, folder)
        info = os.path.join(path, 'info.json')
        if not os.path.isdir(path) or not os.path.isfile(info):
            continue
        with open(info, encoding='utf-8') as fp:
            raw = json.load(fp)
        name = raw.get('name') or folder
        fn, mv = raw.get('file', ''), raw.get('mv', '')
        music = os.path.join(path, fn) if fn and os.path.isfile(os.path.join(path, fn)) else ''
        video = os.path.join(path, mv) if mv and os.path.isfile(os.path.join(path, mv)) else ''
        if not music and not video:
            continue
        entries[name] = {'music': music, 'video': video}
    return entries


class FakeHost:
    """真 HostFacade 只从这里取 menu / app / music_dict / plugin_list。"""

    def __init__(self, root, menu, music_dict):
        self.app = root
        self.menu = menu
        self.music_dict = music_dict
        self.plugin_list = []


def menu_labels(menu):
    labels = []
    try:
        last = menu.index('end')
    except Exception:
        return labels
    if last is None:
        return labels
    for i in range(int(last) + 1):
        try:
            labels.append(menu.entrycget(i, 'label'))
        except Exception:
            pass
    return labels


def main():
    import tkinter
    import ttkbootstrap
    from plugin_sandbox.plugin_sandboxa import (env_box, parse_policy, _HOST_TOKEN)

    with open(os.path.join(PLUGIN, 'plugin.json'), encoding='utf-8') as fp:
        manifest = json.load(fp)
    policy = parse_policy(manifest.get('sandbox'), manifest.get('privilege', []), PLUGIN)
    for w in policy.warnings:
        print(f'    [policy warning] {w}')

    music = load_music()
    music_file = next(iter(music.values()))['music']

    root = ttkbootstrap.Window(themename='solarized-light')
    root.withdraw()
    menu = tkinter.Menu(root)

    box = env_box().create(PLUGIN, manifest.get('name', 'report'), PLUGIN, policy,
                           _host_token=_HOST_TOKEN)

    print('--- 策略边界 ---')
    check(box.can('fs:read', music_file), 'music/ 下的音频可读', os.path.basename(music_file))
    check(box.can('fs:read', os.path.join(PLUGIN, 'plugin.json')),
          '插件自己的目录可读')
    check(not box.can('fs:read', r'C:\Windows\win.ini'),
          r'C:\Windows\win.ini 不可读')
    check(box.can('fs:write', os.path.join(PLUGIN, 'report.json')),
          '插件目录可写(导出用)')
    check(not box.can('fs:write', os.path.join(BASE, 'b.py')),
          'b.py 不可写')
    check(not box.can('net'), '网络是关的')
    check(not box.can('proc', 'echo'), '进程是关的')

    if FAILED:
        print('\n策略边界没过,后面的装载不跑了(否则可能弹授权窗卡住)')
        root.destroy()
        return 1

    print('--- 沙盒内装载 ---')
    box.attach_host(FakeHost(root, menu, music))
    ns = box.namespace
    with open(os.path.join(PLUGIN, 'a.py'), encoding='utf-8') as fp:
        source = fp.read().replace('from b import *', '', 1)
    try:
        exec(compile(source, os.path.join(PLUGIN, 'a.py'), 'exec'), ns)
        check(True, 'a.py 在沙盒里装载成功(import 与 ttkbootstrap 组件都拿得到)')
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(False, 'a.py 在沙盒里装载成功', f'{type(e).__name__}: {e}')
        root.destroy()
        return 1

    labels = menu_labels(menu)
    check('曲库报表' in labels, '菜单项挂上了', str(labels))

    print('--- 沙盒内运行 ---')
    ns['open_report']()
    win = ns['_state']['win']
    if win is not None:
        try:
            win.withdraw()
        except Exception:
            pass
    deadline = time.time() + 30
    while time.time() < deadline and ns['_state']['summary'] is None:
        root.update()
        time.sleep(0.01)
    summary = ns['_state']['summary']
    if summary is None:
        check(False, '扫描在 30 秒内跑完')
    else:
        print(f'    {ns["_state"]["head"].cget("text")}')
        tree = ns['_state']['tree']
        rows = tree.get_children()
        for key in rows:
            print('    ' + ' | '.join(str(v) for v in tree.item(key, 'values')))
        check(len(rows) == len(music), f'表格行数({len(rows)})和曲库({len(music)})一致')
        check(summary['ok'] == len(music), f'全部读出元信息({summary["ok"]}/{len(music)})')
        check(summary['duration_ms'] > 0,
              f'总时长 {summary["duration_ms"] // 1000} 秒')

    print('--- 审计日志 ---')
    events = box.audit(300)
    bad = [e for e in events if str(e.get('action', '')).startswith('violation')]
    for e in bad:
        print(f'    {e}')
    check(not bad, '全程没有 violation(插件没触发任何沙盒拦截)',
          f'{len(bad)} 条' if bad else f'{len(events)} 条记录,全是放行/自查')

    try:
        win.destroy()
    except Exception:
        pass
    root.destroy()

    print()
    if FAILED:
        print(f'{len(FAILED)} 项没过: ' + '; '.join(FAILED))
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
