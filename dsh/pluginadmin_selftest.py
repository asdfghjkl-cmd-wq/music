r"""b.py 里插件管理功能的自测。

四部分:
  A.  Config 的读写/撤销方法 —— 全程用临时配置文件,真 config.json 一个字节都不碰;
  A2. 启用/停用(直接改 plugin.json 的 can_exec)—— 用临时插件目录,真 plugin/ 不碰;
  B.  Tkapp._scan_plugin_dirs / _policy_text —— 拿真实 plugin/ 目录扫一遍;
  C.  plugin_setting 窗口 —— 真 Tk 下建窗口、真的用按钮点几下,看文件和配置对不对。

跑法(项目根目录):
    .\.venv\Scripts\python.exe dsh\pluginadmin_selftest.py
退出码 0 = 通过。
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import b as m

FAILED = []
IDENT = 'd:\\k\\music\\plugin\\report'
REAL_REPORT = os.path.join(BASE, 'plugin', 'report', 'plugin.json')


def check(ok, title, detail=''):
    if not ok:
        FAILED.append(title)
    print(f'[{"PASS" if ok else "FAIL"}] {title}' + (f'  -- {detail}' if detail else ''))


def read_text(path):
    with open(path, 'r', encoding='utf-8', newline='') as fp:
        return fp.read()


def write_text(path, text):
    with open(path, 'w', encoding='utf-8', newline='') as fp:
        fp.write(text)


def fresh_config(path):
    """绕开 __init__:不读真实 config.json,只造一个空的配置对象。"""
    cfg = m.Config.__new__(m.Config)
    cfg.path = path
    cfg._dict = {}
    cfg._aliases = None
    cfg.plugin_dir = os.path.join(BASE, 'plugin')
    cfg.theme = 'test'
    cfg.exit_way = 0
    cfg.allow_plugin = []
    cfg.plugin_unsafe = []
    cfg.plugin_grants = {}
    cfg.plugin_modules = {}
    cfg.plugin_deny = {}
    cfg.start_plugin = True
    cfg.start_sandbox = True
    cfg.close_safe_really_plugin = []
    return cfg


# ---------------------------------------------------------------- A

def test_config(cfg, tmpdir):
    print('--- A. Config 的读写与撤销 ---')
    check(cfg.allow(IDENT) is True and IDENT in cfg.allow_plugin,
          'allow() 把插件加进白名单')
    check(cfg.allow(IDENT) is False, '重复加入返回 False(没有变化)')
    check(cfg.allow(IDENT, False) is True and IDENT not in cfg.allow_plugin,
          'allow(False) 把插件移出白名单')

    data = os.path.join(tmpdir, 'data')
    os.makedirs(data, exist_ok=True)
    sample = os.path.join(data, 'a.txt')
    with open(sample, 'w', encoding='utf-8') as fp:
        fp.write('x')
    expect = os.path.normcase(os.path.realpath(data))

    cfg.grant(IDENT, 'fs:read', sample)
    check(cfg.grants_for(IDENT) == [('fs:read', expect)],
          'grant 把文件归一成它所在的目录', str(cfg.grants_for(IDENT)))
    check(cfg.revoke(IDENT, 'fs:read', expect) is True, 'revoke 精确删掉这一条')
    check(cfg.grants_for(IDENT) == [] and IDENT not in cfg.plugin_grants,
          '撤销后授权清空,空壳也被收走')
    check(cfg.revoke(IDENT, 'fs:read', expect) is False, '撤销不存在的东西返回 False')

    cfg.grant(IDENT, 'net')
    cfg.grant(IDENT, 'proc')
    check(('net', None) in cfg.grants_for(IDENT) and ('proc', None) in cfg.grants_for(IDENT),
          'net / proc 整条能力也能记')
    check(cfg.revoke(IDENT, 'net') is True, '撤销 net')
    check(('net', None) not in cfg.grants_for(IDENT)
          and ('proc', None) in cfg.grants_for(IDENT), '只动了 net,proc 还在')

    cfg.deny(IDENT, 'fs:read', 'X')
    cfg.deny(IDENT, 'fs:read', 'Y')
    check(cfg.denies_for(IDENT) == [('fs:read', 'X'), ('fs:read', 'Y')],
          'deny 记下两条"不再询问"')
    check(cfg.forget_deny(IDENT, 'fs:read', 'X') is True, 'forget_deny 清掉指定一条')
    check(cfg.denies_for(IDENT) == [('fs:read', 'Y')], '另一条不受影响')
    check(cfg.forget_deny(IDENT) is True, 'forget_deny 不给 cap 就清空该插件')
    check(cfg.denies_for(IDENT) == [] and IDENT not in cfg.plugin_deny, '清空后空壳也没了')

    check(cfg.set_modules(IDENT, ['numpy', 'pandas']) is True, 'set_modules 写入清单')
    check(cfg.set_modules(IDENT, ['numpy', 'pandas']) is False, '同样的清单返回 False')
    check(cfg.set_modules(IDENT, ['numpy']) is True
          and cfg.plugin_modules[IDENT] == ['numpy'], 'set_modules 能改小')
    check(cfg.set_modules(IDENT, []) is True and IDENT not in cfg.plugin_modules,
          '空清单等于撤销额外模块')

    check(cfg.set_unsafe(IDENT) is True and IDENT in cfg.plugin_unsafe,
          'set_unsafe 加进"完全不受限"')
    check(cfg.set_unsafe(IDENT, False) is True and IDENT not in cfg.plugin_unsafe,
          'set_unsafe(False) 取消"完全不受限"')
    check(cfg.set_no_sandbox(IDENT) is True
          and IDENT in cfg.close_safe_really_plugin, 'set_no_sandbox 标记无沙盒')

    cfg.allow(IDENT)
    cfg.set_unsafe(IDENT)
    cfg.grant(IDENT, 'proc')
    cfg.deny(IDENT, 'net')
    cfg.set_modules(IDENT, ['numpy'])
    view = cfg.plugin_view(IDENT)
    check(view['allow'] and view['unsafe'] and view['no_sandbox'],
          'plugin_view 汇总勾选状态')
    check(('proc', None) in view['grants'] and ('net', None) in view['denies'],
          'plugin_view 同时带出授权和"不再询问"')
    check(view['modules'] == ['numpy'], 'plugin_view 带出模块清单')

    cfg.save()
    with open(cfg.path, encoding='utf-8') as fp:
        saved = json.load(fp)
    check(saved['plugin_unsafe'] == [IDENT], 'save() 把改动落盘')
    check(saved['plugin_grants'][IDENT]['proc'] is True, '落盘保住了 proc 授权')
    check(saved['close_safe_really_plugin'] == [IDENT], '落盘保住了无沙盒标记')


# ---------------------------------------------------------------- A2

def test_can_exec(tmpdir):
    print('--- A2. 启用/停用 = 直接改 plugin.json 的 can_exec ---')
    work = os.path.join(tmpdir, 'plugfiles')
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, 'plugin.json')

    original = ('{\n'
                '  "name": "demo",\n'
                '  "can_exec": false,\n'
                '  "sandbox": {\n'
                '    "fs_read": ["."],\n'
                '    "net": false\n'
                '  }\n'
                '}\n')
    write_text(path, original)
    changed, detail = m.Tkapp._write_can_exec(path, True)
    text = read_text(path)
    check(changed is True, 'can_exec 从 false 改成 true', detail)
    check(text == original.replace('"can_exec": false', '"can_exec": true'),
          '除了那一个值,文件其余部分逐字符不变')
    check(m.Tkapp._read_can_exec(json.loads(text)) is True, '_read_can_exec 读到 True')

    before = text
    changed, detail = m.Tkapp._write_can_exec(path, True)
    check(changed is False and read_text(path) == before,
          '本来就是目标值,拒绝空写一次', detail)

    write_text(path, '{\n  "name": "demo"\n}\n')
    changed, detail = m.Tkapp._write_can_exec(path, True)
    data = json.loads(read_text(path))
    check(changed and data.get('can_exec') is True and data.get('name') == 'demo',
          '没有 can_exec 字段时能补上,且不丢其它键', detail)

    write_text(path, '{\n  "name": "demo"\n}\n')
    changed, detail = m.Tkapp._write_can_exec(path, False)
    check(changed is False, '字段缺失 + 停用:缺省就是 False,不写文件', detail)

    # 值不是 true/false,但语义上已经"启用":和宿主 `if not self.can_exec` 的判定一致,
    # 提前返回、不碰文件 —— 不去规范化别人手写的非标准写法。
    weird = '{\n  "can_exec": "yes"\n}\n'
    write_text(path, weird)
    changed, detail = m.Tkapp._write_can_exec(path, True)
    check(changed is False and read_text(path) == weird,
          '值非布尔但语义一致时,提前返回且不改文件', detail)

    # 真正危险的是"值非布尔、又必须改":这时不能瞎猜,只能拒绝。
    write_text(path, weird)
    changed, detail = m.Tkapp._write_can_exec(path, False)
    check(changed is False and 'true/false' in detail, 'can_exec 值不是布尔时拒绝', detail)
    check(read_text(path) == weird, '拒绝时文件原样不动')

    write_text(path, '{oops')
    changed, detail = m.Tkapp._write_can_exec(path, True)
    check(changed is False and 'JSON' in detail, 'JSON 非法时拒绝', detail)

    write_text(path, '{\n  "name": "demo",\n  "can_exec": false\n}\n')
    changed, detail = m.Tkapp._write_can_exec(path, True)
    check(changed and json.loads(read_text(path))['can_exec'] is True,
          'can_exec 是最后一个键(后面只有换行)也能改', detail)

    crlf = '{\r\n  "can_exec": false\r\n}\r\n'
    with open(path, 'wb') as fp:
        fp.write(crlf.encode('utf-8'))
    changed, detail = m.Tkapp._write_can_exec(path, True)
    raw = open(path, 'rb').read()
    check(changed and raw == crlf.replace('false', 'true').encode('utf-8'),
          'CRLF 行尾原样保留,没有被换成 LF', repr(raw[:20]))


# ---------------------------------------------------------------- A3

def test_disabled_really_blocks(tmpdir):
    """把开关扳到 false,插件的 init 代码到底还跑不跑 —— 用标记文件验。"""
    print('--- A3. 停用之后 init 真的不执行 ---')
    import ttkbootstrap

    root = ttkbootstrap.Window(themename='solarized-light')
    root.withdraw()

    class Host:
        def __init__(self, root):
            self.app = root

    host = Host(root)
    for folder, can in (('plug_enabled', True), ('plug_disabled', False)):
        full = os.path.join(tmpdir, folder)
        os.makedirs(full, exist_ok=True)
        marker = os.path.join(tmpdir, folder + '.ran')
        init = "open(r'%s','w',encoding='utf-8').write('ran')" % marker
        write_text(os.path.join(full, 'plugin.json'),
                   json.dumps({'name': folder, 'can_exec': can, 'init': init}))
        plugin = m.Plugin_no_sandbox(full)
        plugin.init_env({})
        plugin.init_i(host)
        root.update()
        root.update()
        ran = os.path.isfile(marker)
        check(ran == can,
              f'can_exec={can} 时 init {"执行了" if ran else "没执行"}',
              '有标记文件' if ran else '没有标记文件')
    root.destroy()


# ---------------------------------------------------------------- D

def test_load_now(tmpdir):
    """运行期加载:真沙盒、真装载,装载完必须已经封存。"""
    print('--- D. 运行期加载(真沙盒) ---')
    import ttkbootstrap
    from plugin_sandbox.plugin_sandboxa import env_box, SandboxDenied, _HOST_TOKEN

    plug_dir = os.path.join(tmpdir, 'dynplug')
    os.makedirs(plug_dir, exist_ok=True)
    write_text(os.path.join(plug_dir, 'a.py'), "MARK = 'ran'\n")
    manifest = {'name': 'dynplug', 'can_exec': True, 'init_file': 'a.py',
                'sandbox': {'fs_read': ['.']}}
    write_text(os.path.join(plug_dir, 'plugin.json'),
               json.dumps(manifest, ensure_ascii=False))

    root = ttkbootstrap.Window(themename='solarized-light')
    root.withdraw()
    cfg = fresh_config(os.path.join(tmpdir, 'config3.json'))
    identity = os.path.normcase(os.path.realpath(plug_dir))

    class FakeApp(m.Tkapp):
        def __init__(self, root, cfg, registry):
            self.config = cfg
            self.app = root
            self.plugin_list = []
            self.env_dict = registry
            self.run_play_list = []
            self.run_pause_list = []
            self.run_listbox_list = []

    app = FakeApp(root, cfg, env_box())
    item = {'dir': plug_dir, 'identity': identity, 'name': 'dynplug',
            'folder': 'dynplug', 'manifest': manifest, 'loaded': None}

    ok, detail = app._load_plugin_now(item)
    check(ok, '运行期加载成功', detail)
    check(len(app.plugin_list) == 1, '插件进了 plugin_list')
    if not app.plugin_list:
        root.destroy()
        return
    plugin = app.plugin_list[0]
    check(plugin._box.identity_ok, '身份登记通过')
    check(plugin._box.is_sealed(), '加载完沙盒立刻被封存(不然它是全场唯一还能改策略的)')

    try:
        plugin._box.grant('net', None, _host_token=_HOST_TOKEN)
        rejected = False
    except SandboxDenied:
        rejected = True
    check(rejected, '封存之后再想授权会被拒')

    root.update()
    root.update()
    check(plugin._box.namespace.get('MARK') == 'ran', '插件的 init 真的执行了')

    item['loaded'] = plugin
    again, detail2 = app._load_plugin_now(item)
    check(again is False and '已经在跑' in detail2, '已经装载的不会被加载第二次', detail2)

    root.destroy()


# ---------------------------------------------------------------- E

def make_zip(path, entries, symlink=None):
    import zipfile
    with zipfile.ZipFile(path, 'w') as zf:
        for name, content in entries:
            zf.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, 'C:\\Windows\\win.ini')


def test_zip(tmpdir):
    print('--- E. 插件包(zip)的检查与解压 ---')
    inspect = m.Tkapp._inspect_plugin_zip
    extract = m.Tkapp._extract_plugin_zip
    qzip = os.path.join(BASE, 'dsh', 'q.zip')

    info, error = inspect(qzip)
    check(error is None, 'dsh/q.zip 能通过检查', str(error))
    if info:
        check(info['dirname'] == 'q', '顶层目录名解析正确', info['dirname'])
        check(info['manifest'].get('name') == 'exit_add',
              'manifest 读出来了(自报的名字与目录名不同也对)', str(info['manifest']))
        check(info['count'] == 2, '认到 2 个文件', str(info['count']))

    print('    -- 坏包一律在解压前挡住 --')
    # (条目, 期望的错误关键词)
    bad_cases = [
        ([('q/../evil.txt', 'x'), ('q/plugin.json', '{}')], '目标目录之外'),
        ([('/abs/plugin.json', '{}')], '绝对路径'),
        ([('q/a.py', 'x')], '没有 plugin.json'),
        ([('q/plugin.json', '{oops')], '读不出来'),
        ([('q/plugin.json', '[]')], '不是 JSON 对象'),
        ([('a/plugin.json', '{}'), ('b/x.py', '')], '同一个顶层目录'),
        ([('a<bad/plugin.json', '{}')], '不能用作插件目录名'),
        ([], '没有文件'),
    ]
    for i, (entries, keyword) in enumerate(bad_cases):
        path = os.path.join(tmpdir, f'bad{i}.zip')
        make_zip(path, entries)
        got, err = inspect(path)
        check(got is None and keyword in (err or ''),
              f'拒绝坏包:{entries[0][0] if entries else "(空包)"}', err)

    link = os.path.join(tmpdir, 'link.zip')
    make_zip(link, [('q/plugin.json', '{}')], symlink='q/link')
    got, err = inspect(link)
    check(got is None and '符号链接' in (err or ''), '拒绝符号链接条目', err)

    print('    -- 目录名校验 --')
    for name, want in (('q', True), ('my_plug', True), ('..', False), ('.', False),
                       ('a/b', False), ('a<b', False), ('x.', False), ('', False)):
        check(m.Tkapp._safe_plugin_dirname(name) == want,
              f'_safe_plugin_dirname({name!r}) == {want}')

    print('    -- 真的解压 --')
    target = os.path.join(tmpdir, 'extracted')
    ok, err = extract(qzip, target, 'q')
    check(ok, 'dsh/q.zip 解压成功', err)
    check(os.path.isfile(os.path.join(target, 'plugin.json')), 'plugin.json 解出来了')
    check(os.path.isfile(os.path.join(target, 'a.py')), 'a.py 解出来了')
    with open(os.path.join(target, 'plugin.json'), encoding='utf-8') as fp:
        check(json.load(fp).get('name') == 'exit_add', '解出来的内容正确')

    evil = os.path.join(tmpdir, 'evil.zip')
    make_zip(evil, [('q/../../pwned.txt', 'x'), ('q/plugin.json', '{}')])
    ok, err = extract(evil, os.path.join(tmpdir, 'evil_out'), 'q')
    check(ok is False, '解压循环自己也挡路径穿越(不只靠前面的检查)', err)
    leaked = [p for p in (os.path.join(BASE, 'pwned.txt'),
                          os.path.join(os.path.dirname(BASE), 'pwned.txt'))
              if os.path.exists(p)]
    check(not leaked, '没有文件被写到目标目录之外', str(leaked))


# ---------------------------------------------------------------- F

def test_zip_end_to_end(tmpdir):
    """整条链路:点「加载插件…」→ 选包 → 校验 → 解到 plugin/ → 立刻装载。

    这是唯一一个会往真实 plugin/ 里写东西的测试,所以:a) 包名与现有插件都不冲突,
    b) 无论成败都在 finally 里删掉。q.zip 不能用 —— 它的顶层目录就叫 q,
    而 plugin/q 已经存在,点"覆盖"会毁掉真插件。
    """
    print('--- F. 从 zip 装到 plugin/ 并加载(端到端) ---')
    import shutil as _shutil
    import tkinter.filedialog
    import ttkbootstrap
    from plugin_sandbox.plugin_sandboxa import env_box

    folder = 'dynzip_test'
    target = os.path.join(BASE, 'plugin', folder)
    zip_path = os.path.join(tmpdir, 'dynzip_test.zip')
    make_zip(zip_path, [
        (f'{folder}/plugin.json',
         json.dumps({'name': 'dynzip_test', 'can_exec': True, 'init_file': 'a.py',
                     'sandbox': {'fs_read': ['.']}}, ensure_ascii=False)),
        (f'{folder}/a.py', "MARK = 'ran'\n"),
    ])

    root = ttkbootstrap.Window(themename='solarized-light')
    root.withdraw()
    cfg = fresh_config(os.path.join(tmpdir, 'config4.json'))

    class FakeApp(m.Tkapp):
        """这里**不**覆盖 _scan_plugin_dirs:要走真实的 plugin/ 目录。"""

        def __init__(self, root, cfg, registry):
            self.config = cfg
            self.app = root
            self.plugin_list = []
            self.env_dict = registry
            self.run_play_list = []
            self.run_pause_list = []
            self.run_listbox_list = []

    app = FakeApp(root, cfg, env_box())
    naive = {'v': 0}

    def fake_yesno(message, title=None, parent=None, buttons=None):
        # 只认"装并加载";覆盖之类的分支这次都不该走到
        naive['v'] += 1
        check('覆盖' not in (message or ''), '目标目录不该已存在(不该问覆盖)')
        return '装并加载'

    real_open, real_yesno = (tkinter.filedialog.askopenfilename,
                             ttkbootstrap.Messagebox.yesno)
    tkinter.filedialog.askopenfilename = lambda **kw: zip_path
    ttkbootstrap.Messagebox.yesno = fake_yesno
    try:
        app.plugin_setting()
        root.update()
        ui = [w for w in walk(root) if w.winfo_class() == 'Toplevel'][-1]
        ui.withdraw()
        tree = find_trees(ui)[0]
        before = len(tree.get_children())

        buttons = find_buttons(ui, '加载插件…')
        check(len(buttons) == 1, '找到"加载插件…"按钮')
        if not buttons:
            return
        buttons[0].invoke()
        root.update()
        root.update()

        check(naive['v'] >= 1, '走了确认流程')
        check(os.path.isdir(target), f'插件被解到了 plugin/{folder}')
        check(os.path.isfile(os.path.join(target, 'a.py')), 'a.py 在')
        check(os.path.isfile(os.path.join(target, 'plugin.json')), 'plugin.json 在')

        identity = os.path.normcase(os.path.realpath(target))
        check(tree.exists(identity), '列表里出现了刚装的插件')
        after = len(tree.get_children())
        check(after == before + 1, f'列表多了一行({before} -> {after})')
        check(any(getattr(p, 'identity', None) == identity
                  for p in app.plugin_list), '它已经被装载进当前进程')
        loaded = [p for p in app.plugin_list
                  if getattr(p, 'identity', None) == identity]
        if loaded:
            check(loaded[0]._box.is_sealed(), '即时加载的沙盒同样被封存')
            check(loaded[0]._box.namespace.get('MARK') == 'ran', '插件代码执行了')
    finally:
        tkinter.filedialog.askopenfilename = real_open
        ttkbootstrap.Messagebox.yesno = real_yesno
        try:
            root.destroy()
        except Exception:
            pass
        _shutil.rmtree(target, ignore_errors=True)
    check(not os.path.exists(target), '测试收尾把装进去的插件删干净了')


# ---------------------------------------------------------------- B

def test_scan():
    print('--- B. 插件目录扫描 ---')

    class FakeApp:
        plugin_list = []

    FakeApp._scan_plugin_dirs = m.Tkapp._scan_plugin_dirs

    plugins = FakeApp()._scan_plugin_dirs()
    folders = sorted(p['folder'] for p in plugins)
    check(len(plugins) > 0, f'扫到 {len(plugins)} 个插件', ','.join(folders))
    check('report' in folders, '新写的 report 插件在列表里')
    check(all(p['identity'] == os.path.normcase(os.path.realpath(p['dir']))
              for p in plugins), 'identity 口径与 Plugin 类完全一致')

    report = [p for p in plugins if p['folder'] == 'report'][0]
    text = m.Tkapp._policy_text(report['manifest'])
    check('../../music' in text, '权限摘要里能看到它申请读 music/', text)
    check(report['loaded'] is None, '进程里没装载的插件显示为"未装载"')

    from plugin_sandbox.plugin_sandboxa import parse_policy
    policy = parse_policy(report['manifest'].get('sandbox'),
                          report['manifest'].get('privilege', []), report['dir'])
    check(len(policy.fs_read) >= 2 and not policy.net and not policy.proc,
          '声明能被沙盒解析成真实策略(读两条、不开网络/进程)',
          f'fs_read={policy.fs_read}')


# ---------------------------------------------------------------- C

def walk(widget):
    yield widget
    try:
        children = widget.winfo_children()
    except Exception:
        return
    for child in children:
        yield from walk(child)


def find_buttons(root, text):
    out = []
    for w in walk(root):
        try:
            if w.winfo_class() == 'TButton' and str(w.cget('text')) == text:
                out.append(w)
        except Exception:
            pass
    return out


def find_trees(root):
    return [w for w in walk(root) if w.winfo_class() == 'Treeview']


def make_fake_plugins(root_dir):
    """在临时目录里造两个插件目录,一个开着 can_exec,一个关着。"""
    for folder, can in (('plug_on', True), ('plug_off', False)):
        full = os.path.join(root_dir, folder)
        os.makedirs(full, exist_ok=True)
        write_text(os.path.join(full, 'plugin.json'),
                   '{\n  "name": "%s",\n  "can_exec": %s,\n'
                   '  "sandbox": {\n    "fs_read": ["."]\n  }\n}\n'
                   % (folder, 'true' if can else 'false'))


def ident_of(root_dir, folder):
    return os.path.normcase(os.path.realpath(os.path.join(root_dir, folder)))


def can_exec_of(root_dir, folder):
    with open(os.path.join(root_dir, folder, 'plugin.json'), encoding='utf-8') as fp:
        return json.load(fp).get('can_exec')


def test_window(cfg, tmpdir):
    print('--- C. plugin_setting 窗口 ---')
    import ttkbootstrap

    fake_dir = os.path.join(tmpdir, 'fakeplugin')
    make_fake_plugins(fake_dir)
    real_before = read_text(REAL_REPORT)

    root = ttkbootstrap.Window(themename='solarized-light')
    root.withdraw()

    class FakeApp(m.Tkapp):
        """借 Tkapp 的方法,但把插件来源换成临时目录 —— 测试绝不去动真 plugin/。"""

        def __init__(self, root, cfg, scan_dir):
            self.config = cfg
            self.app = root
            self.plugin_list = []
            self._scan_dir = scan_dir

        def _scan_plugin_dirs(self):
            out = []
            for folder in sorted(os.listdir(self._scan_dir)):
                full = os.path.join(self._scan_dir, folder)
                manifest_path = os.path.join(full, 'plugin.json')
                if not os.path.isfile(manifest_path):
                    continue
                with open(manifest_path, encoding='utf-8') as fp:
                    manifest = json.load(fp)
                out.append({'dir': full,
                            'identity': os.path.normcase(os.path.realpath(full)),
                            'name': manifest.get('name') or folder,
                            'folder': folder, 'manifest': manifest, 'loaded': None})
            return out

    app = FakeApp(root, cfg, fake_dir)
    ident_on = ident_of(fake_dir, 'plug_on')
    ident_off = ident_of(fake_dir, 'plug_off')

    cfg.allow(ident_on)
    cfg.grant(ident_on, 'proc')
    cfg.deny(ident_on, 'net')
    cfg.set_modules(ident_on, ['numpy'])
    cfg.set_unsafe(ident_on)

    try:
        app.plugin_setting()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(False, '窗口能建起来', f'{type(e).__name__}: {e}')
        root.destroy()
        return
    check(True, '窗口能建起来')
    root.update()

    windows = [w for w in walk(root) if w.winfo_class() == 'Toplevel']
    if not windows:
        check(False, '找到插件管理窗口')
        root.destroy()
        return
    ui = windows[0]
    try:
        ui.withdraw()
    except Exception:
        pass

    trees = find_trees(ui)
    check(len(trees) == 2, '窗口里有插件列表和权限明细两张表', f'{len(trees)} 张')
    if len(trees) != 2:
        root.destroy()
        return
    tree, detail = trees

    rows = tree.get_children()
    check(len(rows) == 2, f'列表列出临时目录里的 2 个插件,实际 {len(rows)}')
    check(tree.exists(ident_on) and tree.exists(ident_off), '两个插件都在列表里')

    check(tree.item(ident_on, 'values')[0] == '✓'
          and tree.item(ident_off, 'values')[0] == '',
          '启用列读的是 can_exec(开着的打勾,关着的空)',
          f'{tree.item(ident_on, "values")[0]!r} / {tree.item(ident_off, "values")[0]!r}')
    check(tree.item(ident_off, 'values')[2] == '未装载',
          '状态列(index 2)说的是装载情况,和 can_exec 无关',
          str(tree.item(ident_off, 'values')))

    tree.selection_set(ident_on)
    root.update()
    kinds = [detail.item(k, 'values') for k in detail.get_children()]
    shown = {str(v[0]) for v in kinds}
    check('完全不受限' in shown and '已授权' in shown
          and '不再询问' in shown and '额外模块' in shown,
          '明细里四类条目都出来了', str(sorted(shown)))

    buttons = find_buttons(ui, '启用/停用')
    check(len(buttons) == 1, '找到"启用/停用"按钮')
    if buttons:
        buttons[0].invoke()
        root.update()
        check(can_exec_of(fake_dir, 'plug_on') is False,
              '点一下把 plugin.json 的 can_exec 改成了 false')
        check(tree.item(ident_on, 'values')[0] == '',
              '列表上的勾跟着消失了(刷新过)')
        check(can_exec_of(fake_dir, 'plug_off') is False,
              '只动了选中的那个,另一个没被牵连')

    buttons = find_buttons(ui, '免询问开关')
    check(len(buttons) == 1, '找到"免询问开关"按钮')
    if buttons:
        buttons[0].invoke()
        root.update()
        check(ident_on not in cfg.allow_plugin, '免询问开关把插件移出了 allow_plugin')
        check(can_exec_of(fake_dir, 'plug_on') is False,
              '免询问开关不碰 can_exec(两个开关各管各的)')

    detail.selection_set(detail.get_children()[0])
    root.update()
    buttons = find_buttons(ui, '撤销选中权限')
    if buttons and detail.selection():
        buttons[0].invoke()
        root.update()
        check(ident_on not in cfg.plugin_unsafe, '撤销"完全不受限"生效')

    buttons = find_buttons(ui, '清空"不再询问"')
    if buttons:
        buttons[0].invoke()
        root.update()
        check(cfg.denies_for(ident_on) == [], '清空"不再询问"生效')

    with open(cfg.path, encoding='utf-8') as fp:
        saved = json.load(fp)
    check(ident_on not in saved.get('allow_plugin', []), '窗口里的配置改动确实落盘了')

    buttons = find_buttons(ui, '加载插件…')
    check(len(buttons) == 1, '窗口里有"加载插件…"按钮')

    check(read_text(REAL_REPORT) == real_before,
          '全程没有碰真实插件的 plugin.json')

    try:
        ui.destroy()
    except Exception:
        pass
    root.destroy()


def main():
    # 刻意不用 tempfile.mkdtemp:它在 Windows 上用 os.mkdir(path, 0o700) 建目录,
    # 会把这层目录的权限整体换成"只有 owner",工作区继承来的沙盒授权项一起没了 ——
    # 那样建出来的目录,沙盒令牌在里面连个子目录都建不了、最后连自己都删不掉。
    # 用默认权限建在工作区里,收尾再删掉。
    tmpdir = os.path.join(BASE, '.pluginadmin-test')
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    try:
        cfg = fresh_config(os.path.join(tmpdir, 'config.json'))
        test_config(cfg, tmpdir)
        test_can_exec(tmpdir)
        test_disabled_really_blocks(tmpdir)
        test_load_now(tmpdir)
        test_zip(tmpdir)
        test_zip_end_to_end(tmpdir)
        test_scan()
        cfg2 = fresh_config(os.path.join(tmpdir, 'config2.json'))
        test_window(cfg2, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if FAILED:
        print(f'{len(FAILED)} 项没过: ' + '; '.join(FAILED))
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
