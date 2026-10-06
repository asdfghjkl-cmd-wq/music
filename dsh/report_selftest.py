# -*- coding: utf-8 -*-
r"""plugin/report 的自测:不启动播放器、不开 Tk,只验它能验的部分。

覆盖三件事:
  1. plugin/report/collect.py 的纯逻辑(格式化边界 + 拿真实 music/ 目录算一遍);
  2. plugin/report/plugin.json 是合法 JSON,而且声明了读 music/ 的权限;
  3. plugin/report/a.py 能编译,并且没有 import 沙盒白名单之外的模块。

跑法(项目根目录):
    .\.venv\Scripts\python.exe dsh\report_selftest.py
退出码 0 = 全部通过,1 = 有失败项。
"""
import importlib.util
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(BASE, 'plugin', 'report')
FAILED = []


def check(ok, title, detail=''):
    mark = 'PASS' if ok else 'FAIL'
    if not ok:
        FAILED.append(title)
    print(f'[{mark}] {title}' + (f'  -- {detail}' if detail else ''))


def load_collect():
    spec = importlib.util.spec_from_file_location(
        'report_collect', os.path.join(PLUGIN, 'collect.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_entries():
    """照 b.py load_music() 的规则读一遍 music/,拿真实数据喂报表。"""
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


def main():
    collect = load_collect()
    import mutagen

    print('--- 格式化边界 ---')
    check(collect.human_ms(0) == '--:--', 'human_ms(0) 是占位符')
    check(collect.human_ms(59_000) == '00:59', 'human_ms(59000) -> 00:59',
          collect.human_ms(59_000))
    check(collect.human_ms(3_723_000) == '1:02:03', 'human_ms 超过一小时带小时位',
          collect.human_ms(3_723_000))
    check(collect.human_ms('x') == '--:--', 'human_ms 收到非数字不炸')
    check(collect.human_size(0) == '-' and collect.human_size(None) == '-',
          'human_size 零/None 都是占位符')
    check(collect.human_size(4_194_188) == '4.0 MB', 'human_size(4194188) -> 4.0 MB',
          collect.human_size(4_194_188))

    print('--- 真实曲库 ---')
    entries = load_entries()
    check(len(entries) > 0, f'music/ 里读到 {len(entries)} 首', ', '.join(entries))
    summary = collect.collect(entries, mutagen.File, os.path.getsize)
    for t in summary['tracks']:
        state = collect.human_ms(t['length_ms']) if t['ok'] else f'失败:{t["error"]}'
        print(f'    {t["name"]:<12} {t["kind"]:<4} {state:<22} '
              f'{t["format"]:<8} {collect.human_size(t["size"])}')
    print(f'    汇总: 共 {summary["total"]} 首 / 读到 {summary["ok"]} 首 / '
          f'总时长 {collect.human_ms(summary["duration_ms"])} / '
          f'总体积 {collect.human_size(summary["bytes"])} / 格式 {summary["by_format"]}')
    check(summary['total'] == len(entries), '报表条数和曲库条数一致')
    check(summary['ok'] > 0, '至少读出一首的元信息')
    check(summary['duration_ms'] > 0, '总时长有值')
    check(summary['bytes'] > 0, '总体积有值')

    print('--- 坏文件不会带崩整张表 ---')
    def boom(_path):
        raise OSError('故意炸的')
    broken = collect.collect({'坏文件': {'music': 'x.mp3', 'video': ''}}, boom)
    check(broken['total'] == 1 and broken['failed'] == 1 and broken['ok'] == 0,
          'opener 抛异常时记成失败,不往上冒')
    check('故意炸的' in broken['tracks'][0]['error'], '原因写进了 error 字段',
          broken['tracks'][0]['error'])
    empty = collect.collect({'空项': {'music': '', 'video': ''}}, mutagen.File)
    check(empty['failed'] == 1, '没有文件的项也算失败一行')

    print('--- 导出结构 ---')
    payload = collect.to_json_obj(summary, '2026-01-01 00:00:00')
    dumped = json.dumps(payload, ensure_ascii=False)
    check(payload['total'] == summary['total'], 'to_json_obj 保住总数')
    check(len(payload['tracks']) == summary['total'], '导出的曲目数对得上')
    check('"length_text"' in dumped and '"size_text"' in dumped,
          '导出可序列化(中文没被转义)')
    round_trip = json.loads(dumped)
    check(round_trip['tracks'] and 'length_text' in round_trip['tracks'][0],
          '导出对象能 JSON 往返,每行带格式化后的时长')
    check(isinstance(payload['by_format'], dict), '格式分布是普通 dict')

    print('--- 插件声明 ---')
    with open(os.path.join(PLUGIN, 'plugin.json'), encoding='utf-8') as fp:
        manifest = json.load(fp)
    check(manifest.get('name') == 'report', 'manifest 有 name')
    policy = manifest.get('sandbox') or {}
    check('../../music' in (policy.get('fs_read') or []),
          'fs_read 声明了 music/ 目录', str(policy.get('fs_read')))
    check(policy.get('proc') is False and policy.get('net') is False,
          '不开进程和网络')

    print('--- 插件代码 ---')
    with open(os.path.join(PLUGIN, 'a.py'), encoding='utf-8') as fp:
        source = fp.read()
    try:
        compile(source, 'a.py', 'exec')
        check(True, 'a.py 能编译')
    except SyntaxError as e:
        check(False, 'a.py 能编译', str(e))
    allowed = {'collect', 'json', 'os', 'time', 'tkinter', 'tkinter.messagebox',
               'ttkbootstrap', 'mutagen', 'math', 'random', 're', 'string',
               'textwrap', 'traceback', 'logging', 'datetime', 'collections',
               'itertools', 'functools', 'io', 'sys'}
    imported = []
    for line in source.splitlines():
        line = line.strip()
        if line.startswith('import ') and ' from ' not in line:
            imported += [p.strip().split(' as ')[0] for p in line[7:].split(',')]
    bad = [n for n in imported if n not in allowed]
    check(not bad, 'a.py 只 import 沙盒放行的模块',
          f'越界: {bad}' if bad else ', '.join(imported))

    print()
    if FAILED:
        print(f'{len(FAILED)} 项没过: ' + '; '.join(FAILED))
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
