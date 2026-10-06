from threading import Thread
import tkinter,io,time,random,queue,platform,os,functools,sys,copy,traceback,json,logging,zipfile,tkinter.filedialog

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if platform.system() == 'Windows':
    os.environ['PYTHON_VLC_MODULE_PATH'] = f"{BASE_DIR}/pvlc"
try:
    from PIL import ImageTk,Image
    import pystray,ttkbootstrap,vlc
except ModuleNotFoundError:
    logging.error(f'不是哥们,你是就安装了个python吗,给我去执行"pip install -r {BASE_DIR}{os.sep}requirements.txt"')
    sys.exit(1)
start_music_message = True
try:
    import mutagen
    import mutagen.flac
    import mutagen.id3
except ModuleNotFoundError:
    traceback.print_exc()
    logging.warning('mutagen不存在,无法加载音乐信息')
    start_music_message = False

def _is_within(base,candidate):
    """candidate 的 realpath 是否落在 base 的 realpath 之内(含 base 自身)。

    用 os.path.commonpath 而不是字符串前缀:前缀比较会让 plugin/ab 匹配到
    plugin/abc。commonpath 在不同盘符/不同 UNC 共享上会抛 ValueError,那种情况
    一定不是"之内",连同路径为空一起按越界处理。大小写用 normcase 抹平
    (Windows 不区分大小写),realpath 负责解开软链接/8.3 短名。
    """
    if not base or not candidate:
        return False
    try:
        b = os.path.normcase(os.path.realpath(base))
        c = os.path.normcase(os.path.realpath(candidate))
        return os.path.commonpath([b,c]) == b
    except (OSError,ValueError,TypeError) as e:
        logging.debug(f'路径包含校验失败 {base!r} <- {candidate!r}:{e}')
        return False

def _close_quietly(obj,what):
    """关掉 mutagen/PIL 对象(Windows 上不关会一直占着文件句柄)。

    关闭失败只记 debug:收尾动作不能把播放流程带挂。
    """
    close = getattr(obj,'close',None)
    if close is None:
        return
    try:
        close()
    except Exception as e:
        logging.debug(f'关闭 {what} 失败(忽略):{e}')

_DEFAULT_PHOTO = None
_DEFAULT_PHOTO_TRIED = False

def _default_photo():
    """a.png 的模块级惰性单例:只 Image.open 一次,之后复用同一个对象。

    返回 None 表示封面不可用(文件缺失/损坏)。这里绝不抛异常 —— 原来
    load_message 里的裸调用一旦抛出去,set_media_path 就走不到 return True,
    调用方以为失败,表现成"点了不播"。单例也顺手避免了每次切歌都新开一个
    句柄又只关掉上一个的泄漏。
    """
    global _DEFAULT_PHOTO, _DEFAULT_PHOTO_TRIED
    if _DEFAULT_PHOTO_TRIED:
        return _DEFAULT_PHOTO
    _DEFAULT_PHOTO_TRIED = True
    try:
        _DEFAULT_PHOTO = Image.open(os.path.join(BASE_DIR,'a.png'))
        # B9:Image.open 只读文件头、并不解码,所以"打开成功"不等于"能用"。
        # 真解码发生在 flush_display 的 src.resize(...),而那里没有保护 ——
        # 一个头部合法但数据损坏的 a.png 会在切歌路径上抛,正是本函数承诺
        # 不会发生的事。这里先强制解码一次,把坏文件在这里就降级成 None。
        _DEFAULT_PHOTO.load()
    except Exception:
        logging.exception('打开默认封面 a.png 失败,本次按无封面处理')
        _DEFAULT_PHOTO = None
    return _DEFAULT_PHOTO

class Config:

    # M11:start_sandbox 默认 True —— 插件默认在沙盒里跑。
    # 设成 False 等于把插件代码直接 exec 在宿主环境上,不再有任何判权与询问,
    # 是一次彻底的信任降级,必须由用户在设置里显式选择(且需重启才生效)。
    DEFAULT = {'theme': 'solarized-light', 'exit_way': 0,'allow_plugin':[],
               'plugin_unsafe':[],'plugin_grants':{},'plugin_modules':{},
               # L14:记住用户点过的"不再询问"(插件身份 -> 能力/目标)。
               # 没有它,这个选择只在本次进程内有效,重启之后又问一遍。
               'plugin_deny':{},
               'start_plugin':True,'start_sandbox':True,'close_safe_really_plugin':[]}

    @classmethod
    def _default(cls,key):
        """取默认值的副本,避免 DEFAULT 里的可变对象(如白名单列表)被泄漏出去后就地改写"""
        return copy.deepcopy(cls.DEFAULT[key])

    def __init__(self):
        self.path = os.path.join(BASE_DIR,'config.json')
        # 插件目录,以及「历史旧名 -> identity」的懒加载映射(见 _identity_aliases)
        self.plugin_dir = os.path.join(BASE_DIR,'plugin')
        self._aliases = None
        self._dict = copy.deepcopy(self.DEFAULT)
        self.theme = self._default('theme')
        self.exit_way = self._default('exit_way')
        self.allow_plugin = self._default('allow_plugin')
        self.plugin_unsafe = self._default('plugin_unsafe')
        self.plugin_grants = self._default('plugin_grants')
        self.plugin_modules = self._default('plugin_modules')
        self.plugin_deny = self._default('plugin_deny')
        self.start_plugin = self._default('start_plugin')
        self.start_sandbox = self._default('start_sandbox')
        self.close_safe_really_plugin =  self._default('close_safe_really_plugin')
        self.load()
    
    @staticmethod
    def _plugin_candidates(plugin_dir):
        """扫出 plugin/ 下每个插件目录的「候选旧名」集合 -> {候选名: identity}。

        历史配置(config.json)里 allow_plugin / plugin_unsafe / plugin_grants /
        plugin_deny / plugin_modules 的键曾是插件**自报的 name**,现在授权一律按
        identity(目录 realpath)。不做这一步换算,老用户的白名单会在升级后整体
        失效、每次启动重问一遍。

        这里刻意**不调用** Plugin 类:Config 会在 Plugin 定义之前就被实例化
        (模块级 _boot_config),引过去会拿到未绑定的名字。所以按同样的口径
        重建:候选名取「目录名」与 plugin.json 里的 name;重名时依次让出
        name (2)、name (3)……,和 Plugin.__init__ 的消歧规则保持一致。
        """
        out = {}
        try:
            names = sorted(os.listdir(plugin_dir))
        except OSError:
            return out
        taken = set()
        for entry in names:
            full = os.path.join(plugin_dir, entry)
            if not os.path.isdir(full):
                continue
            if not os.path.isfile(os.path.join(full, 'plugin.json')):
                # 必须与装载循环同口径(load_music 里"没有 plugin.json 就 continue")。
                # 否则一个没有 plugin.json 的目录会白占一个候选名、还会把 taken
                # 推高一格,于是后面真正的插件算出的 final 与 Plugin 的消歧结果
                # 错位,历史配置里的插件名就换算不到正确的 identity。
                continue
            declared = ''
            try:
                with open(os.path.join(full, 'plugin.json'), 'r', encoding='utf-8') as fp:
                    manifest = json.load(fp)
                if isinstance(manifest, dict):
                    raw = manifest.get('name', '')
                    if isinstance(raw, str) and raw:
                        declared = raw
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                declared = ''
            base = declared or os.path.basename(os.path.normpath(full))
            final, i = base, 2
            while final in taken:
                final = f'{base} ({i})'
                i += 1
            taken.add(final)
            ident = os.path.normcase(os.path.realpath(full))
            for cand in (base, final, os.path.basename(os.path.normpath(full))):
                if cand:
                    out.setdefault(cand, ident)
        return out

    def _identity_aliases(self):
        if self._aliases is None:
            self._aliases = self._plugin_candidates(
                self.plugin_dir or os.path.join(BASE_DIR, 'plugin'))
        return self._aliases

    def _migrate_who(self, who):
        """把历史配置里的插件名换算成 identity;换算不了就原样返回。

        已经是 identity 的值会如实命中 _plugin_candidates 里同一个目录,
        于是换算后与自身相等,不会破坏现有(已是路径的)配置。
        """
        if not isinstance(who, str) or not who:
            return who
        return self._identity_aliases().get(who, who)

    def _migrate_value(self, value):
        """数组类配置项(allow_plugin / plugin_unsafe)的逐项换算 + 去重。

        换算不出来的项**保持原样**:它可能是已经卸载的插件(历史上被允许过),
        留着既不影响判定,也不会因为一次读盘就把用户的历史记录抹掉。
        """
        if not isinstance(value, list):
            return value
        out = []
        for item in value:
            if not isinstance(item, str) or not item:
                continue
            got = self._identity_aliases().get(item, item)
            if got not in out:
                out.append(got)
        return out

    def load(self):
        # 每次(重新)装载都重扫一次插件目录,别让上一次的旧名->identity 映射
        # 残留到已经变了的磁盘状态上
        self._aliases = None
        # 先把可变状态复位:同一个 Config 对象二次 load(或换了文件)时,
        # 文件里缺的字段必须回到默认值,而不是留着上一次读到的内容
        self.theme = self._default('theme')
        self.exit_way = self._default('exit_way')
        self.allow_plugin = self._default('allow_plugin')
        self.plugin_unsafe = self._default('plugin_unsafe')
        self.plugin_grants = self._default('plugin_grants')
        self.plugin_modules = self._default('plugin_modules')
        self.plugin_deny = self._default('plugin_deny')
        self.start_plugin = self._default('start_plugin')
        self.start_sandbox = self._default('start_sandbox')
        self.close_safe_really_plugin = self._default('close_safe_really_plugin')
        try:
            with open(self.path,'r',encoding='utf-8-sig') as fp:
                n = json.load(fp)
        except FileNotFoundError:
            n = {}                      
        except (OSError,UnicodeDecodeError,json.JSONDecodeError) as e:
            logging.exception('读取配置文件失败,按默认配置处理')
            # 坏配置留一份现场再按默认值走,不然下次保存会把它直接覆盖掉
            # (FileNotFoundError 是 OSError 子类,上面已经先接住了,不会走到这里)
            self._backup_broken_config()
            print(f'读取 {self.path} 失败,按默认配置处理:{e}')
            n = {}
        if not isinstance(n,dict):
            print(f'{self.path} 的内容不是 JSON 对象,按默认配置处理')
            self._backup_broken_config()
            n = {}
        self._dict = n

        t = n.get('theme',None)
        p = n.get('allow_plugin',None)
        if p is None:
            # 兼容历史配置里的旧键名,避免用户的白名单被静默清空
            for legacy in ('plugin','sussess_plugin','success_plugin'):
                if legacy in n:
                    print(f'{self.path} 使用了旧键 {legacy},已按 allow_plugin 处理')
                    p = n[legacy]
                    break
        self.theme = t if isinstance(t,str) and t else self._default('theme')

        if isinstance(p,list):
            bad = [x for x in p if not (isinstance(x,str) and x)]
            if bad:
                print(f'{self.path} 里的 allow_plugin 有 {len(bad)} 个无效项,已忽略:{bad!r}')
            # 空列表是合法值:表示不自动加载任何插件。
            # L16:逐项把历史配置里的「插件名」换算成 identity —— 授权键早已
            # 改成目录 realpath(H1:自报 name 不能再决定授权归属),但 config 里
            # 存量的是名字,不换算就等于把用户的白名单静默清空。
            self.allow_plugin = self._migrate_value(p)
        else:
            if p is not None:
                print(f'{self.path} 里的 allow_plugin 不是数组,按默认值处理')
            self.allow_plugin = self._default('allow_plugin')

        c = n.get('close_safe_really_plugin')
        if isinstance(c,list):
            bad = [x for x in c if not (isinstance(x,str) and x)]
            if bad:
                print(f'{self.path} 里的 close_safe_really_plugin 有 {len(bad)} 个无效项,已忽略:{bad!r}')
            # 空列表是合法值:表示不自动加载任何插件。
            # L16:逐项把历史配置里的「插件名」换算成 identity —— 授权键早已
            # 改成目录 realpath(H1:自报 name 不能再决定授权归属),但 config 里
            # 存量的是名字,不换算就等于把用户的白名单静默清空。
            self.close_safe_really_plugin = self._migrate_value(c)
        else:
            if c is not None:
                print(f'{self.path} 里的 close_safe_really_plugin 不是数组,按默认值处理')
            self.close_safe_really_plugin = self._default('close_safe_really_plugin')

            
        u = n.get('plugin_unsafe',None)
        if isinstance(u,list):
            self.plugin_unsafe = self._migrate_value(u)
        elif u is not None:
            print(f'{self.path} 里的 plugin_unsafe 不是数组,按默认值处理')
            self.plugin_unsafe = self._default('plugin_unsafe')
        self.start_plugin = n.get('start_plugin',self._default('start_plugin'))
        self.start_sandbox = n.get('start_sandbox',self._default('start_sandbox'))
        if type(self.start_plugin) != bool:self.start_plugin = self._default('start_plugin')
        if type(self.start_sandbox) != bool:self.start_sandbox = self._default('start_sandbox')

        g = n.get('plugin_grants',None)
        if isinstance(g,dict):
            # 只收:插件名 -> {fs_read/fs_write: [路径], net/proc: true}
            # 键按 identity 存(与 _plugin_persist 写盘时的口径一致)
            self.plugin_grants = {}
            for who,item in g.items():
                if not isinstance(who,str) or not who or not isinstance(item,dict):
                    print(f'{self.path} 里的 plugin_grants {who!r} 无效,已忽略')
                    continue
                who = self._migrate_who(who)
                one = {}
                for key in ('fs_read','fs_write'):
                    v = item.get(key,None)
                    if v is None:
                        continue
                    if isinstance(v,str):
                        v = [v]
                    if not isinstance(v,list):
                        print(f'{self.path} 里 {who} 的 {key} 不是数组,已忽略')
                        continue
                    paths = []
                    for p in v:
                        if isinstance(p,str) and p.strip():
                            # 和沙盒侧 _grant_root/_norm 对齐:abspath+normpath+
                            # normcase+realpath。只做 normpath 的话相对路径会在
                            # 沙盒的 cwd 下被重新解释,和用户当初授权的目录不是一回事。
                            paths.append(os.path.normcase(
                                os.path.realpath(os.path.normpath(os.path.abspath(p)))))
                        else:
                            print(f'{self.path} 里 {who} 的 {key} 有无效项 {p!r},已忽略')
                    if paths:
                        one[key] = paths
                for key in ('net','proc'):
                    v = item.get(key,None)
                    if v is None:
                        continue
                    if not isinstance(v,bool):
                        print(f'{self.path} 里 {who} 的 {key} 不是布尔值,已忽略')
                        continue
                    if v:
                        one[key] = True
                if one:
                    self.plugin_grants[who] = one
        elif g is not None:
            print(f'{self.path} 里的 plugin_grants 不是对象,按默认值处理')
            self.plugin_grants = self._default('plugin_grants')

        d = n.get('plugin_deny',None)
        if isinstance(d,dict):
            # 与 plugin_grants 同构:插件身份 -> {fs_read/fs_write: [目标], net/proc: true}。
            # 注意:目标**原样收下、不做二次归一化** —— 沙盒存进来的是它自己用
            # _grant_root() 算过的值,_ask_key 对"根"再算一次是幂等的,重新归一化
            # 反而可能和沙盒的算法算歪,导致"不再询问"恢复不上。
            self.plugin_deny = {}
            for who,item in d.items():
                if not isinstance(who,str) or not who or not isinstance(item,dict):
                    print(f'{self.path} 里的 plugin_deny {who!r} 无效,已忽略')
                    continue
                who = self._migrate_who(who)
                one = {}
                for key in ('fs_read','fs_write'):
                    v = item.get(key,None)
                    if v is None:
                        continue
                    if isinstance(v,str):
                        v = [v]
                    if not isinstance(v,list):
                        print(f'{self.path} 里 {who} 的 {key} 不是数组,已忽略')
                        continue
                    targets = [p for p in v if isinstance(p,str) and p.strip()]
                    if targets:
                        one[key] = targets
                for key in ('net','proc'):
                    if item.get(key):
                        one[key] = True
                if one:
                    self.plugin_deny[who] = one
        elif d is not None:
            print(f'{self.path} 里的 plugin_deny 不是对象,按默认值处理')
            self.plugin_deny = self._default('plugin_deny')

        m = n.get('plugin_modules',None)
        if isinstance(m,dict):
            # 只收:插件身份(目录 realpath) -> [确认放行的额外模块名]
            self.plugin_modules = {}
            for who,item in m.items():
                if not isinstance(who,str) or not who or not isinstance(item,list):
                    print(f'{self.path} 里的 plugin_modules {who!r} 无效,已忽略')
                    continue
                who = self._migrate_who(who)
                names = list(dict.fromkeys(x for x in item
                                           if isinstance(x,str) and x.strip()))
                if len(names) != len(item):
                    print(f'{self.path} 里 {who} 的 plugin_modules 有无效项,已忽略')
                if names:
                    self.plugin_modules[who] = names
        elif m is not None:
            print(f'{self.path} 里的 plugin_modules 不是对象,按默认值处理')
            self.plugin_modules = self._default('plugin_modules')

        try:
            self.exit_way = int(n.get('exit_way',self._default('exit_way')))
        except (TypeError,ValueError):
            logging.exception('解析配置项 exit_way 失败')
            print(f'{self.path} 里的 exit_way 不是数字,按默认值处理')
            self.exit_way = self._default('exit_way')
        if self.exit_way not in (0,1,2):
            print(f'{self.path} 里的 exit_way={self.exit_way} 已失效,按默认值处理')
            self.exit_way = self._default('exit_way')

    def grants_for(self,name):
        """把 config 里记住的授权摊平成 (能力,目标) 列表,装载时喂回沙盒。"""
        g = self.plugin_grants.get(name) or {}
        out = []
        for p in g.get('fs_read',()) or ():
            out.append(('fs:read',p))
        for p in g.get('fs_write',()) or ():
            out.append(('fs:write',p))
        if g.get('net'):
            out.append(('net',None))
        if g.get('proc'):
            out.append(('proc',None))
        return out

    def denies_for(self,name):
        """把 config 里记住的"不再询问"摊平成 (能力,目标) 列表,装载时喂回沙盒。

        L14:沙盒侧一直有 set_persist_deny / remember_denied 这一对入口,宿主却
        从来没接 —— 于是"不再询问"只在本次进程内有效(靠进程里的 _never_ask),
        重启之后同一个插件又会被完整地问一遍。
        """
        g = self.plugin_deny.get(name) or {}
        out = []
        for p in g.get('fs_read',()) or ():
            out.append(('fs:read',p))
        for p in g.get('fs_write',()) or ():
            out.append(('fs:write',p))
        if g.get('net'):
            out.append(('net',None))
        if g.get('proc'):
            out.append(('proc',None))
        return out

    def grant(self,name,cap,target=None):
        """记一条授权:插件运行期点"总是允许"时由播放器调用,随后由 save() 落盘。"""
        g = self.plugin_grants.setdefault(name,{})
        if cap in ('fs:read','fs:write'):
            key = 'fs_read' if cap == 'fs:read' else 'fs_write'
            if not target:
                return g
            # 沙盒的白名单是"目录级"的:这里归一成真正生效的目录,
            # 免得 config.json 里记着一个文件路径、实际放开的却是整个目录。
            # 与沙盒侧 _grant_root 同口径:已存在的文件取父目录,
            # 不存在或本身是目录就原样(不上浮,免得把授权悄悄放大)。
            target = os.path.normcase(
                os.path.realpath(os.path.normpath(os.path.abspath(target))))
            if os.path.isfile(target):
                target = os.path.dirname(target) or target
            g.setdefault(key,[])
            if target not in g[key]:
                g[key].append(target)
        elif cap in ('net','proc'):
            g[cap] = True
        return g

    def deny(self,name,cap,target=None):
        """记一条"不再询问":插件运行期点"不再询问"时由播放器调用,随后 save() 落盘。

        与 grant 的关键差别:这里**不做归一化**。沙盒传进来的 target 已经是它
        自己用 _grant_root() 算好的值,原样存、原样喂回去,_ask_key 两侧的算法
        才一致;再归一化一次就可能对不上,表现成"明明记了却还是弹窗"。
        """
        g = self.plugin_deny.setdefault(name,{})
        if cap in ('fs:read','fs:write'):
            key = 'fs_read' if cap == 'fs:read' else 'fs_write'
            if not target:
                return g
            lst = g.setdefault(key,[])
            if target not in lst:
                lst.append(target)
        elif cap in ('net','proc'):
            g[cap] = True
        return g

    # ---------------- 插件管理窗口用的读写 ----------------
    # grant/deny 负责"记下一条授权",这里负责另一半:精确撤销。用户点的是
    # "撤销这一条",不是"清空这个插件",所以每个方法只动目标条目,并且在条目
    # 空掉之后顺手把空壳收掉 —— 否则 config.json 里会留一堆 {} 说明不了任何事。

    def allow(self,name,flag=True):
        """把插件加入/移出装载白名单(allow_plugin)。返回配置是否真的变了。"""
        if flag and name not in self.allow_plugin:
            self.allow_plugin.append(name)
            return True
        if not flag and name in self.allow_plugin:
            self.allow_plugin.remove(name)
            return True
        return False

    def set_unsafe(self,name,flag=True):
        """plugin_unsafe:完全不受沙盒限制的名单。"""
        if flag and name not in self.plugin_unsafe:
            self.plugin_unsafe.append(name)
            return True
        if not flag and name in self.plugin_unsafe:
            self.plugin_unsafe.remove(name)
            return True
        return False

    def set_no_sandbox(self,name,flag=True):
        """close_safe_really_plugin:名单里的插件不走沙盒直接跑(信任降级)。"""
        if flag and name not in self.close_safe_really_plugin:
            self.close_safe_really_plugin.append(name)
            return True
        if not flag and name in self.close_safe_really_plugin:
            self.close_safe_really_plugin.remove(name)
            return True
        return False

    def revoke(self,name,cap,target=None):
        """撤销一条授权。target=None 表示整条能力(net/proc)。"""
        g = self.plugin_grants.get(name)
        if not g:
            return False
        if cap in ('fs:read','fs:write'):
            key = 'fs_read' if cap == 'fs:read' else 'fs_write'
            lst = g.get(key) or []
            if target not in lst:
                return False
            lst.remove(target)
            if not lst:
                g.pop(key,None)
        elif cap in ('net','proc'):
            if g.pop(cap,None) is None:
                return False
        else:
            return False
        if not g:
            self.plugin_grants.pop(name,None)
        return True

    def forget_deny(self,name,cap=None,target=None):
        """清"不再询问":给了 cap 就清那一条,不给就整个插件清空。"""
        g = self.plugin_deny.get(name)
        if not g:
            return False
        if cap is None:
            self.plugin_deny.pop(name,None)
            return True
        if cap in ('fs:read','fs:write'):
            key = 'fs_read' if cap == 'fs:read' else 'fs_write'
            lst = g.get(key) or []
            if target not in lst:
                return False
            lst.remove(target)
            if not lst:
                g.pop(key,None)
        elif cap in ('net','proc'):
            if g.pop(cap,None) is None:
                return False
        else:
            return False
        if not g:
            self.plugin_deny.pop(name,None)
        return True

    def set_modules(self,name,modules):
        """写插件额外模块的放行清单;空清单等于撤销。"""
        modules = [str(m) for m in (modules or []) if str(m)]
        old = list(self.plugin_modules.get(name) or [])
        if modules:
            if old == modules:
                return False
            self.plugin_modules[name] = modules
            return True
        if name in self.plugin_modules:
            self.plugin_modules.pop(name,None)
            return True
        return False

    def plugin_view(self,name):
        """摊平一个插件的全部授权,给管理窗口显示与逐条撤销用。"""
        return {'allow': name in self.allow_plugin,
                'unsafe': name in self.plugin_unsafe,
                'no_sandbox': name in self.close_safe_really_plugin,
                'grants': self.grants_for(name),
                'denies': self.denies_for(name),
                'modules': list(self.plugin_modules.get(name) or [])}

    def _backup_broken_config(self):
        """把读不出来的配置文件挪到带时间戳的 .bak,备份失败也不影响启动。

        用 os.replace 而不是 os.rename:同名 .bak 已存在时也能覆盖,不挑平台。
        整个动作包在 try 里 —— 备份失败只提示一句,不会让程序启动即崩。
        注意调用方负责保证文件确实存在(FileNotFoundError 分支不该调这里)。
        """
        dst = f'{self.path}.{time.strftime("%Y%m%d-%H%M%S")}.bak'
        try:
            os.replace(self.path,dst)
        except OSError as e:
            logging.exception('备份配置文件失败,忽略')
            print(f'备份 {self.path} 失败(忽略):{e}')

    def save(self):
        # 清掉已经迁移过的旧键,免得它们一直留在配置文件里
        for legacy in ('plugin','sussess_plugin','success_plugin'):
            self._dict.pop(legacy,None)
        self._dict['theme'] = self.theme
        self._dict['exit_way'] = self.exit_way
        self._dict['allow_plugin'] = self.allow_plugin
        self._dict['plugin_unsafe'] = self.plugin_unsafe
        self._dict['plugin_grants'] = self.plugin_grants
        self._dict['plugin_modules'] = self.plugin_modules
        self._dict['plugin_deny'] = self.plugin_deny
        self._dict['start_sandbox'] = self.start_sandbox
        self._dict['start_plugin'] = self.start_plugin
        self._dict['close_safe_really_plugin'] = self.close_safe_really_plugin

        tmp = self.path + '.tmp'
        try:
            with open(tmp,'w',encoding='utf-8') as fp:
                json.dump(self._dict,fp,ensure_ascii=False)
                # 原子替换只保证"读者要么看到旧文件要么看到新文件",不保证内容
                # 已经落盘:掉电/崩溃后可能留下一个被截断的 config.json,下次
                # 启动就被判成"损坏",白名单与授权被整体重置。替换前先刷盘。
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp,self.path)
        except OSError as e:
            logging.exception('保存配置文件失败')
            print(f'保存 {self.path} 失败:{e}')


def _setup_logging():
    """把日志写进 music.log。

    只在作为主程序运行时调用 —— 本文件也会被插件 import,那时候不该去动调用方
    的 logging 配置。L13:必须在读 config.json **之前**装好,否则"配置解析失败"
    这类最需要留痕的报错会落在启动的空窗里,music.log 里什么都看不到。
    """
    logging.basicConfig(level=logging.DEBUG,
                        filename=os.path.join(BASE_DIR,'music.log'),
                        encoding='utf-8',
                        format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s')

# L13:作为主程序启动时,先把日志装好,再去读配置
if __name__ == '__main__':
    _setup_logging()

# L12:原来这里 new 了两次 Config() —— 两个实例各读一遍 config.json,
# 既浪费也可能读到不一致的中间态。配置只有一份,读一次。
_boot_config = Config()
start_plugin = _boot_config.start_plugin
start_sandbox = _boot_config.start_sandbox
# 插件沙盒:能力模型、路径判定、封存与审计都在 plugin_sandbox.py 里。
# env_box 由它提供,替代原先那个把 b.py 的 globals() 整个暴露给插件的实现。

if start_plugin and start_sandbox:
    try:
        from plugin_sandbox.plugin_sandboxa import (env_box, parse_policy, SandboxDenied, _HOST_TOKEN,
                                ASK_YES, ASK_SESSION, ASK_ALWAYS, install_audit_hook,SandboxView,ASK_NEVER)
    except ModuleNotFoundError:
        traceback.print_exc()
        logging.warning('plugin_sandbox文件不存在,无法加载插件')
        start_plugin = False



NO_FILE = 'no<>found*.mp3*.mp4..'

def _plugin_data_path(dir,rel,key):
    """把 plugin.json 里的 init_file/command_file 解析成一个安全的绝对路径。

    返回 None 表示拒绝读取(越界,或路径本身有问题)。插件可以用
    '../..' / 绝对路径 / 软链接把自己的"代码文件"指到插件目录之外,
    那等于绕过沙盒的来源判定去加载任意本地文件,所以越界一律不读,
    只打印一条带越界路径和插件目录的警告。
    """
    try:
        target = os.path.join(dir,rel)
    except TypeError as e:
        print(f'{key}={rel!r} 无法拼成路径,已忽略:{e}')
        return None
    if not _is_within(dir,target):
        print(f'[沙盒] 警告:{key}={rel!r} 越出插件目录,已拒绝读取')
        # 诊断打印本身也不能抛:realpath 在 Windows 上遇到超长/非法路径会抛
        # OSError,那样"拒绝读取"的路径反而变成一次未捕获异常。
        for _label,_value in (('插件目录',dir),('越界路径',target)):
            try:
                _shown = os.path.realpath(_value)
            except Exception:
                _shown = _value
            print(f'[沙盒]   {_label}:{_shown}')
        return None
    return target


def _plugin_share_env(manifest,name):
    """读 plugin.json 的 share_env:只有显式 true 才算声明,其余一律按"不共享"。

    它决定这个插件能不能用自报的 env_id 去和别的插件共用同一份环境。默认关:
    "自报 env_id 即可共用"是一条隐式通道(两个目录不同的插件写同一个 env_id 就能
    挤进同一份命名空间),现在必须显式声明才放行。
    """
    if not isinstance(manifest,dict):
        return False
    raw = manifest.get('share_env',False)
    if raw is True:
        return True
    if raw not in (False,None):
        print(f'插件 {name} 的 share_env 不是布尔值({raw!r}),按不共享处理')
    return False


def _shared_env_key(manifest):
    """声明了 share_env 的插件用哪个键共享环境:自报 env_id,没写就用 'shared'。"""
    raw = manifest.get('env_id','') if isinstance(manifest,dict) else ''
    if isinstance(raw,str) and raw:
        return raw
    return 'shared'


class Plugin_no_sandbox:
    def __init__(self,dir:str):

        self.plugin_file_path = os.path.join(dir,'plugin.json')
        n = {}
        try:
            with open(self.plugin_file_path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except Exception as e:
            print(f'读取 {self.plugin_file_path} 失败,按空配置处理:{e}')
        if not isinstance(n,dict):
            print(f'{self.plugin_file_path} 的内容不是 JSON 对象,按空配置处理')
            n = {}
        
        self.init = n.get('init','').replace('from b import *','',1)
        init_file = n.get('init_file','')
        if init_file:
            p = _plugin_data_path(dir,init_file,'init_file')
            if p is not None:
                try:
                    with open(p,'r',encoding="utf-8") as fp:
                        self.init = fp.read().replace('from b import *','',1)
                except Exception as e:
                    print(f'读取 {init_file} 失败,忽略:{e}')

        self.command = n.get('command','')
        command_file = n.get('command_file','')
        if command_file:
            p = _plugin_data_path(dir,command_file,'command_file')
            if p is not None:
                try:
                    with open(p,'r',encoding="utf-8") as fp:
                        self.command = fp.read()
                except Exception as e:
                    print(f'读取 {command_file} 失败,忽略:{e}')

        self.init_ok = False
        
        self.com = None

        self.name = n.get('name','')
        self.can_exec = n.get('can_exec',False)
        self.exec_path = n.get('exec_path',[])
        # 插件身份 = 插件目录的规范化 realpath(与沙盒版 Plugin 同一口径)。
        # 装载循环(load_music)对两种插件统一读 n.identity 做白名单判断与落盘,
        # 本类以前只设了 env_id —— 关掉沙盒后每个插件都会在 n.identity 处抛
        # AttributeError,被外层 except 吞成"加载插件 X 失败,已跳过",插件全装不上。
        self.identity = os.path.normcase(os.path.realpath(dir))
        # 环境命名空间默认按插件目录独立(identity)。只有 plugin.json 里显式写了
        # "share_env": true,才按自报的 env_id(缺省 'shared')与别的插件共用同一份
        # 命名空间 —— 没声明的插件即使写了 env_id 也不再共用(那条隐式通道已关闭)。
        self.share_env = _plugin_share_env(n,self.name)
        # M12:env_id 不能默认 ''。多个插件都用 '' 时 env_dict.get('') 会拿到
        # **同一份**命名空间,插件之间互相覆盖变量;identity 天然唯一。
        self.env_id = _shared_env_key(n) if self.share_env else self.identity
        
    def init_env(self,env_dict:dict):
        box = env_dict.get(self.env_id,None)
        if box is None:
            # 无沙盒模式下插件本来就要能拿到宿主环境(这是该模式的既定语义,
            # 删掉会废掉所有老插件),但至少给每个插件各自一份,不与他人共用。
            box = dict(globals())
            msg = (f'插件 {self.name} 以【无沙盒】模式运行,'
                   f'可直接访问宿主环境,不受任何权限限制')
            logging.warning(msg)
            print('[警告] ' + msg)
        self.env_dict = box
        self.env_dict['__env_id__']  = self.env_id
        self.env_dict['__start_sandbox__'] = start_sandbox
        env_dict[self.env_id] =self.env_dict


        
    def init_i(self,tkaapp):
        self.env_dict['pro'] = tkaapp
        
        if not self.can_exec or self.init_ok:
            return
        try:
            # 先编译:语法错误在这里就能拿到,不用等回调里再炸
            code = compile(self.init,f'<plugin {self.name} init>','exec')
        except Exception as e:
            print(f'插件 {self.name} 的 init 代码无法编译,已跳过:{e}')
            return
        # L8:init_ok 只能在编译成功之后置位。原来放在 compile 之前,编译一失败
        # 它已经是 True —— run() 于是照样去执行 command 代码,在一个连 init 都
        # 没跑起来的环境里运行插件逻辑。
        self.init_ok = True
        def _run_init():
            # 插件代码出问题只应该影响它自己,不能把整个播放器带崩
            try:
                exec(code,self.env_dict)
            except Exception as e:
                print(f'插件 {self.name} 初始化失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_init)


    def run(self,tkaapp=None):
        if not self.can_exec or tkaapp is None:
            return
        # L8 的注释声称"init 没起来就不执行 command",但 run() 从来没读 init_ok:
        # init 编译失败时 init_ok 保持 False 也拦不住这里,command 照样会在一个
        # init 从未执行过的命名空间里 exec(通常直接 NameError)。装载流程
        # (load_q/load_p)保证 init_i() 一定在 run() 之前被调用,所以直接判它即可。
        if not self.init_ok:
            return
        self.env_dict['pro'] = tkaapp
        if not self.com:
            try:
                self.com = compile(self.command,f'<plugin {self.name} command>','exec')
            except Exception as e:
                print(f'插件 {self.name} 的 command 无法编译,已跳过:{e}')
                return
        
        
        
        def _run_command():
            try:
                exec(self.com,self.env_dict)
            except Exception as e:
                print(f'插件 {self.name} 执行失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_command)

# M14:状态文字走队列用的哨兵。Player._pending 里正常压的是
# (path,end_action,gen,failed) 四元组;这里用模块级对象占第一位,
# 不会与任何真实路径相等,也不受"代"(gen)作废的影响。
_STATUS = object()

class Player:
    def __init__(self,d:dict): 
        params = [
                        "--network-caching=3000",  
                        "--avcodec-hw=any",       
                        "--codec=avcodec",         
                        "--verbose=-1"
                    ]
        self.player= vlc.Instance(" ".join(params))

        self.music_photo = None
        # 当前 set_mrl 得到的 Media 对象。libvlc 的 set_mrl 每次都会新建一个,
        # 拿不住引用就没法释放(不释放会一直占着文件句柄/解码资源),
        # 所以这里必须自己记着,换曲和退出时显式 release。
        self._media = None
        # cleanup() 是否已经跑过。release() 之后再碰 music_player 会调用到已经
        # 析构的 libvlc 对象 —— 那是进程级崩溃,不是能 catch 的 Python 异常,
        # 所以 SeekBar 的轮询也用这个标志当硬护栏。
        self._released = False
        # 供 cleanup() 显式停掉进度条轮询(Tkapp 建好 SeekBar 后回填)。
        self.seekbar = None

        self.play_tt = 0

 
        
        
        self.music_message = {'name': ''}
        self._pending: "queue.Queue" = queue.Queue()
        self._pumping = False

        self._play_started = 0.0
        # 短播计数按曲目键(_gate_key)分别记。原来是一个全局整数,于是"三首各自
        # 只播了 0.3 秒的正常短音效(音效/试听切片)"会凑够 3,把第三首本身没
        # 问题的歌拉黑并弹窗。
        self._short_plays = {}
        # 上一次挂监听时用的 (path, end_action)。重扫列表会摘掉监听,
        # 需要靠它把"放完自动切歌"接回去(见 rearm_listen)。
        self._last_listen = None

        self._bad: "set" = set()
        # 已经为哪些曲目弹过"是否不允许播放"的确认框。
        # 这一条是防死循环用的:同一个坏文件如果不记住"问过了",
        # 用户每按一次"否",下面 match 里的重放就会在同一轮里再触发一次
        # EncounteredError,于是弹窗→否→重放→弹窗,永远出不去。
        # 用户手动播放(start_current)时会清空,所以想重新问是能问到的。
        self._prompted: "set" = set()
        # 已经问过并处理完的那一首(按是→拉黑,按否→保留但不自动重放)。
        # 同一首的后续事件一律丢掉,直到用户重新起播才解除。
        self._settled = ''
 
        self._gen = 0
        # 当前播放的是条目里的哪一路:'music' 或 'video'。
        # 自动切歌按这个字段取路径,避免在 MV 模式下切到音频文件。
        self.media_key = 'music'
        self.ml = None
        self.music_player = vlc.MediaPlayer(self.player)
        self.music_dict = d

        self.listb: "ttkbootstrap.Listbox | None" = None
        self._listening = False
    def add_listen(self,event,func,*args):
        self.music_player.event_manager().event_attach(event,func,*args)
    def del_listen(self,event):
        self.music_player.event_manager().event_detach(event)
    def play_jump_not_stop(self):
        # 拖动进度条之后,按"上次开始播放的时刻"算很容易被误判成短播
        # (见 _advance 里的三次拉黑),这里用一个远早于任何 time.monotonic()
        # 的值把计时起点顶开。
        self._play_started = -64.0
    def _stop_listen(self):

        self._gen += 1
        if not self._listening:
            return
        self._listening = False
        for ev in (vlc.EventType.MediaPlayerEndReached,
                   vlc.EventType.MediaPlayerEncounteredError):
            try:
                self.del_listen(ev)
            except Exception as e:
                logging.exception('摘除事件监听失败')
                print(f'摘除 {ev} 监听失败(忽略):{e}')

    def _listen(self, path, end_action):
        # 正常放完(EndReached)和打不开/解不开(EncounteredError)都必须接管:
        # 只接前者的话,遇到坏文件自动切歌会永久停在那里,不会跳到下一首。
        self.add_listen(vlc.EventType.MediaPlayerEndReached,
                        self._on_end_reached, path, end_action, self._gen)
        self.add_listen(vlc.EventType.MediaPlayerEncounteredError,
                        self._on_encountered_error, path, end_action, self._gen)
        self._last_listen = (path,end_action)
        self._listening = True


    def rearm_listen(self,end_action=None):
        # flush_music() 重扫列表时会摘掉监听(旧条目已经无效)。如果当前这首
        # 还在新列表里,必须把 EndReached/EncounteredError 接回去,否则这首歌
        # 放完就停住,再也不会自动切歌。路径以新列表为准。
        if self._last_listen is None:
            return False
        old_path,old_action = self._last_listen
        path = self._playable(self.music_message.get('name',''))
        if self.music_player.get_state() not in (vlc.State.Playing,vlc.State.Paused):
            # 窗口期:flush_music() 是"先 stop_listen() 摘掉监听 → 重建列表 →
            # 再调本函数"。EndReached 如果恰好落在这两步之间,事件已经丢了,
            # 此刻状态是 Ended/Stopped —— 原来这里直接 return False,于是那首歌
            # 放完就永远停住,正是上面注释承诺要避免的情况。
            # 这种情况照样补挂一次:只要这一首还在新列表里(path 非空)。
            # 事件已经丢了、不会重放,多挂一次监听没有副作用。
            if not path:
                return False
        self._listen(path or old_path,
                     end_action if end_action is not None else old_action)
        return True

    def set_dict_and_tk_obj(self,d:dict,l:"ttkbootstrap.Listbox",ml):
        self.music_dict = d
        self.listb = l
        self.ml = ml
        # B8:_bad / _prompted 里存的是 _gate_key() 的结果 —— 曲目名缺失时它
        # 会退化成路径,而 d.keys() 是曲目名。原来直接用 set(d.keys()) 取交集,
        # 会把那些路径形态的键整批丢掉:重扫列表后,之前拉黑的坏文件重新回到
        # 候选里,重新失败、重新弹窗。这里按 _gate_key 的真实口径转换。
        keep = set()
        for name in d:
            if name:
                keep.add(name)
            else:
                path = self._track_path(name)
                if path and path != NO_FILE:
                    keep.add(path)
        self._bad &= keep
        self._prompted &= keep
        if not isinstance(self._short_plays,dict):
            self._short_plays = {}
        self._short_plays = {k:v for k,v in self._short_plays.items() if k in keep}
        # B8 的同一口径:_settled 里存的也可能是路径(_gate_key 在名字缺失时的
        # 退化结果),拿它跟 d(键是曲目名)比会永远不命中,于是每次重扫都把
        # "已处理"这扇闸门静默清空。与 _bad/_prompted 一样按 keep 比。
        if self._settled and self._settled not in keep:
            self._settled = ''
        if not self._pumping:
            self._pumping = True
            # M8:不要在这里**同步**跑 _pump。set_dict_and_tk_obj 是在 flush_music
            # 重建列表**之前**被调用的,同步处理队列里压着的切歌事件会读到旧列表。
            # 投到事件循环的下一拍,等列表重建完再处理。
            lb = self.listb
            if lb is not None:
                try:
                    lb.after(0,self._pump)
                except tkinter.TclError:
                    logging.exception('启动播放事件轮询失败')
                    self._pumping = False
    def set_play_way(self,aint):
        
        if 0<=aint<=2:self.play_tt = aint
    def _on_end_reached(self,event,path,end_action,gen):
        # VLC 在自己的线程里回调,这里只投队列,真正的切歌在 Tk 线程的 _pump 里做
        self._pending.put((path,end_action,gen,False))

    def _on_encountered_error(self,event,path,end_action,gen):
        # "打不开/解不开"是确定性证据,标记 failed 让 _advance 直接拉黑这一首
        self._pending.put((path,end_action,gen,True))

    def _pump(self):
        
        lb = self.listb
        if lb is None:
            # 列表控件还没准备好。这里必须放开 _pumping,否则 set_dict_and_tk_obj
            # 之后再也不会启动轮询,队列里的事件就永远没人处理了。
            self._pumping = False
            return
        try:
            while True:
                path,end_action,gen,failed = self._pending.get_nowait()
                if path is _STATUS:
                    # 状态文字只是给用户看的提示,不受"代"作废影响
                    self._apply_status(end_action)
                    continue
                if gen != self._gen:
                    
                    
                    continue
                try:
                    self._advance(path,end_action,failed)
                except Exception:
                    # L5:静默吞掉会让"下一首永远不来"变成无迹可寻的故障。
                    # 至少让用户看见,并把现场留在日志里。
                    logging.exception('处理播放结束事件失败')
                    self._apply_status('播放流程出错,详见日志')
                    
        except queue.Empty:
            pass
        try:
            lb.after(50,self._pump,)
        except tkinter.TclError:
            # 控件已经销毁/不可用:必须放开 _pumping,否则 set_dict_and_tk_obj
            # 之后再也不会重启轮询,队列里的事件就永远没人处理了(静默失效)。
            logging.exception('重新调度事件轮询失败,已暂停轮询')
            self._pumping = False

    def _select_index(self,index):
        lb = self.listb
        if lb is None:
            return
        lb.selection_clear(0,tkinter.END)
        lb.selection_set(index)
        lb.see(index)

    def _ask_keep(self,name,message):
        # 弹"是否不允许播放"的确认框。
        # 返回 True = 用户说不允许播放(该拉黑);False = 允许播放,别拉黑。
        # 取消/关窗(返回值不是"是")按"允许播放"处理,和原来 == '是' 的语义一致。
        # 必须显式传 buttons:这个参数是 keyword-only,漏了会直接 TypeError。
        try:
            ans = ttkbootstrap.Messagebox.yesno(message,'player',buttons=['是','否'])
        except Exception:
            logging.exception('弹出确认框失败,按允许播放处理')
            
            return False
        return ans == '是'

    def _track_path(self,name):
        """取某首曲目在当前媒体类型(音频/视频)下的路径,可能为空。"""
        try:
            return self.music_dict.get(name,{}).get(self.media_key,'') or ''
        except Exception:
            logging.exception('取曲目路径失败')
            return ''

    def _gate_key(self,name,path=None):
        """L10/L11:`_bad` / `_prompted` 这两个拦截闸门用的键。

        它们原来直接拿曲目名当键。曲目名**可能为空**(标签缺失、只按路径列出来的
        情形),空串会让所有无名曲目共用一个键:只要其中一首被拉黑,
        `_playable` 就会对后面每一首都返回 '',单曲循环也会被误停。
        名字缺失时退回用路径 —— 路径对一首曲目是唯一的。

        名字非空时返回值与原来**完全一致**,所以具备名字的曲目行为不变。
        """
        if path is None:
            path = self._track_path(name)
        return name or path

    def _stop(self):
        # stop() 摘监听时会 _gen += 1,但 _pending 里可能还压着同一份坏媒体的
        # EncounteredError 事件。这里再推一次 _gen,让队列里的旧事件在 _pump
        # 里被 gen 检查丢掉 —— 否则"停下来"之后又被旧事件叫起来重放同一首。
        self.stop()
        self._gen += 1

    def _advance(self,path,end_action,failed=False):
        name = self.music_message.get('name') or ''
        # L10/L11:闸门键与展示名分开。名字缺失时 key 会退回路径,
        # 避免所有无名曲目共用 '' 而互相污染拉黑/弹窗记忆。
        key = self._gate_key(name,path)
        label = name or path
        # 已经在本次播放里问过并处理过这一首了:
        # VLC 对一个坏文件会连着抛好几个 EncounteredError,而且"回答否→重放"
        # 本身就会再造一个新事件。没有这个闸门,事件会一轮轮喂回来,
        # 弹窗/重放就停不下来 —— 这就是按"否"后无限循环的根。
        # 只有用户手动重新起播(start_current)才会解锁,到时可以再问。
        if key and key == self._settled:
            return
        # 下面收集"这一轮要不要弹窗、弹什么",弹窗只做一次。
        # 按"否"= 允许播放(不拉黑),但事件链必须在这里断掉,不再重放。
        ask = None
        if failed:
            # 打开/解码失败是确定性证据,不用等短播计数攒够三次,直接拉黑
            if key and key not in self._prompted:
                self._prompted.add(key)
                ask = '无法打开音频,可能出现了问题,是否不允许播放'
            self._short_plays.pop(key,None)
        elif time.monotonic() - self._play_started < 0.5:
            # 按曲目键分开计数(见 __init__ 的说明),不再用全局整数。
            n = self._short_plays.get(key,0) + 1
            self._short_plays[key] = n
            if n >= 3:
                # 数够了就清零:不管回答是"是"还是"否",都不能带着 3 这个计数
                # 往下走,否则下一次事件又会立刻满足 >=3 再弹一次。
                self._short_plays.pop(key,None)
                if key and key not in self._prompted:
                    self._prompted.add(key)
                    ask = '音频可能出现问题,是否不允许播放'
        else:
            self._short_plays.pop(key,None)

        if ask is not None:
            if self._ask_keep(label,ask):
                self._bad.add(key)
                print(f'{label} 无法正常播放,自动切歌不再选它')
                self._settled = key
                self._stop()
                # 顺序很重要:_stop() 内部会 _set_status('无')。原来把这句提示
                # 放在它前面,刚压进队列就被"无"顶掉,用户实际看不到失败原因。
                self._set_status('播放失败:文件无法播放')
                if self.play_tt in (1,2):
                    # 拉黑之后没必要再重放这一首:顺序/随机模式直接切下一首。
                    self._advance_to(self._next_seq() if self.play_tt == 1 else self._next_random(),end_action)
                return
            # 回答"否":用户认定文件没问题,允许播放 —— 不拉黑,但这一轮的
            # 事件链到此为止(重放同一个坏文件只会立刻再失败一次,再弹一次框)。
            # 也把弹窗记忆去掉,用户手动重播时可以重新问,不会被静默拉黑。
            self._settled = key
            self._prompted.discard(key)
            print(f'{label} 播放异常,已按用户选择保留,停止自动重放,可手动重试或换一首')
            self._stop()
            # 同上:_stop() 会写"无",这行必须排在它后面才看得见。
            self._set_status('播放异常:已停止自动重放')
            return

        match self.play_tt:
            case 0:       
                if key in self._bad:
                    
                    
                    print(f'{label} 无法播放,单曲循环已停止,请换一首或检查文件')
                    self._set_status('播放失败:文件无法播放')
                    # M9:单曲循环遇到坏文件、决定放弃时,原来只推了 gen 却没摘
                    # 监听 —— 监听器会一直挂着,而 _listening 还停在 True
                    # (状态不一致,后续 _listen 的判断会跟着错)。走 _stop_listen。
                    self._stop_listen()          
                    return
                ok = self._replay(name,path,end_action)
                self._after_switch(ok,path,end_action)
                
            case 1:
                
                self._advance_to(self._next_seq(),end_action)
                
            case 2:
                
                self._advance_to(self._next_random(),end_action)
            case _:
                print(f'未知播放模式:{self.play_tt}')

            
                        


    def _replay(self,name,path,end_action):
        # 按当前模式重放指定的这一首:音频走 set_media_path,视频走 set_media_path_mv
        if self.media_key == 'video':
            return self.set_media_path_mv(name,path,end_action)
        return self.set_media_path(name,path,end_action)

    def _playable(self,name):
        
        # L10/L11:闸门键统一走 _gate_key,免得无名曲目在这里被误判成"已拉黑"
        if self._gate_key(name) in self._bad:
            return ''
        p = self.music_dict.get(name,{}).get(self.media_key,'')
        return p if p and p != NO_FILE else ''

    def _next_seq(self):

        keys = list(self.music_dict.keys())
        if not keys:
            return None
        n = self.music_message.get('name','')
        start = keys.index(n) if n in keys else -1
        for i in range(1,len(keys)+1):
            cand = keys[(start+i) % len(keys)]
            if cand == n:
                # 绕了整整一圈又回到自己:说明确实没有别的可播,
                # 顺序播放不该把自己当成"下一首"反复重放。
                continue
            if self._playable(cand):
                return cand
        return None

    def _next_random(self):

        cand = [x for x in self.music_dict if self._playable(x)]
        n = self.music_message.get('name','')
        if len(cand) > 1 and n in cand:
            cand.remove(n)
        if not cand:
            return None
        return random.choice(cand)
    def stop_listen(self):
        self._stop_listen()
    def _advance_to(self,name,end_action):
        
        # _next_seq/_next_random 已经过滤过拉黑名单,这里再兜一次底:
        # 任何情况下都不自动切到已知放不出来的曲目,否则会来回空转。
        _key = self._gate_key(name) if name is not None else ''
        if _key and _key in self._bad:
            print(f'{name} 已被标记为无法播放,忽略这次自动切歌')
            name = None
        if name is None:
            print('没有可播放的下一首,自动切歌停止')
            self._set_status('无可播放的下一首')
            # M9:这里原本是裸的 _gen += 1 —— 只作废了事件代次,却没摘掉 VLC 的
            # 事件监听,监听器会一直挂着而 _listening 还停在 True(状态不一致,
            # 后续 _listen 也会因为 _listening 判断而漏挂/错挂)。走 _stop_listen。
            self._stop_listen()
            return
        path = self._playable(name)
        ok = self._replay(name,path,end_action)
        # M8:先真正开始播放,再做界面选中。
        # 原来 _select_index 在前且毫无保护:它一旦抛异常(TclError 等),后面的
        # _after_switch 就永远不执行 —— 用户看到的表现就是"点了却不播"。
        # 界面选中只是锦上添花,绝不能挡住播放这条主路径。
        self._after_switch(ok,path,end_action)
        if ok:
            try:
                keys = list(self.music_dict.keys())
                if name in keys:
                    self._select_index(keys.index(name))
            except Exception:
                logging.exception('切歌后同步选中项失败(忽略)')
    def set_volume(self,volume):
        self.music_player.audio_set_volume(volume)
    def _warn_no_file_soon(self):
        """L6:延迟弹出"未指定文件"提示。

        原来直接写 `self.listb.after(50, lambda: ... parent=self.listb.master)`:
        既没有判空(self.listb 在控件就绪前是 None),lambda 又是 50ms 后才求值的,
        那时 self.listb 可能已经变成 None —— 一个提示弹窗反而把播放流程打断。
        这里先把控件抓在手里并判空。
        """
        lb = self.listb
        if lb is None:
            logging.warning('未指定文件,但列表控件尚未就绪,跳过提示')
            return
        try:
            lb.after(50,lambda: ttkbootstrap.Messagebox.show_warning('未指定文件',parent=lb.master))
        except tkinter.TclError:
            logging.exception('投递"未指定文件"提示失败')

    def _set_status(self,text):
        # M14:`after(0)` 不是线程安全边界 —— 从非 Tk 线程调 widget.after() 本身
        # 就是未定义行为(Tk 不是线程安全的),拿它当"切回主线程"的手段是错的。
        # 改成投进 _pending,由 Tk 线程上的 _pump 统一消费并写控件
        # (与托盘命令走 _ui_queue 是同一个模式)。控件还没就绪也没关系,
        # 事件先排队,等 _pump 起来再逐条落到控件上。
        self._pending.put((_STATUS,text,self._gen,False))

    def _apply_status(self,text):
        # 只在 Tk 线程上被 _pump 调用
        ml = self.ml
        if ml is None:
            return
        try:
            ml.config(text=text)
        except tkinter.TclError:
            logging.exception('更新状态文字失败')

    def start_current(self):
        # 播放已经 set_mrl 好的媒体,并把短播计时的起点重置到此刻。
        # 给 _after_switch 和 Tkapp 用,外部就不必再去改 _play_started 了。
        # L9:libvlc 起不来时 play() 是**返回 -1**,不是抛异常。原来直接把返回值
        # 丢掉,用户看到的就是"点了没反应、也没有任何提示"。这里接住并如实告知。
        if self.play() == -1:
            logging.warning('libvlc 拒绝开始播放(play() 返回 -1):%r',
                            self.music_message.get('name'))
            self._set_status('播放失败:播放器无法启动')
            return False
        self._play_started = time.monotonic()
        # 这里是"用户/流程明确要开播当前这一首"的唯一入口(选曲、切模式、
        # 自动切歌成功都会走到),所以在这里清掉弹窗记忆与"已处理"闸门:
        #   - 用户手动重播同一首时,坏文件还能再问一次,不至于永远静默;
        #   - 自动切歌进了新的一首,那一首的弹窗记忆也不需要留着。
        self._prompted.clear()
        self._settled = ''
        return True

    def _after_switch(self,ok,path,end_action):
        if not ok:
            
            
            
            print(f'切歌失败,自动播放已停止:{path!r}')
            self._set_status('切歌失败')
            return
        self.start_current()
        if end_action is not None:
            end_action()

    def _set_media(self,path):
        """set_mrl 并接住返回的 Media,释放上一次的。

        原来丢掉了 set_mrl 的返回值,libvlc 每次新建的 Media 就再没人释放,
        换曲越多占得越多。释放旧对象放在赋新值之前,失败只记日志 ——
        释放失败不该让这次切歌失败。
        """
        old = self._media
        self._media = self.music_player.set_mrl(path)
        if old is not None:
            try:
                old.release()
            except Exception:
                logging.exception('释放上一个 Media 失败(忽略)')
        return self._media

    def _release_media(self):
        media = self._media
        self._media = None
        if media is not None:
            try:
                media.release()
            except Exception:
                logging.exception('释放 Media 失败(忽略)')

    def _set_cover(self,pic_data=None):
        """统一设置封面:pic_data 为 None 时回到 a.png 的惰性单例。

        这里绝不抛异常:封面拿不到最多是没图,不能连带把"点了不播"带出来。
        注意不再 close 上一张封面:原来在 load_message 里无条件
        `self.music_photo.close()`,而切换失败时 self.music_photo 又会被赋成
        默认封面单例,下一次就把它关掉了 —— 关掉的是一个全局缓存对象,
        之后再没人能重建它(PIL 的 close 不会抛,所以失败是静默的)。
        旧封面交给 GC 回收即可,显示用的 PhotoImage 由 Tkapp 自己持有。
        """
        if pic_data is None:
            self.music_photo = _default_photo()
            return
        try:
            photo = Image.open(io.BytesIO(pic_data))
            # 与 _default_photo 的 B9 修复同一口径:Image.open 只读文件头,头部
            # 合法但数据被截断的封面不会在这里抛,异常会被推迟到 flush_display 的
            # src.resize(...) —— 那是切歌路径上的裸调用,一旦抛出会顶掉
            # _after_switch 之后的收尾。这里先强制解码一次,把坏封面就地降级。
            photo.load()
            self.music_photo = photo
        except Exception:
            logging.exception('解码内嵌封面失败,回退到默认封面')
            self.music_photo = _default_photo()

    def set_media_path_mv(self,name,path,end_action=None):
        if not path or path == NO_FILE:
            self._warn_no_file_soon()
            return False
        self._stop_listen()            
        # M9:_stop_listen() 内部已经无条件地 _gen += 1(在 _listening 判断之前),
        # 这里原来又推了一次 —— 重复自增,纯冗余。
        self._set_media(path)
        # 标签/封面的来源显式给出:优先用同名音乐文件的标签;
        # 没有可用的音乐文件时传 None(而不是把 NO_FILE 哨兵当路径去读标签),
        # 由 load_message 统一负责重置信息与封面。
        tag_path = self.music_dict.get(name,{}).get('music',None)
        if not tag_path or tag_path == NO_FILE or not os.path.isfile(tag_path):
            tag_path = None
        self.media_key = 'video'
        self.load_message(name,tag_path)
        if end_action is not None:
            self._listen(path,end_action)
        # load_message 保证不抛:封面失败也必须按"切歌成功"返回,
        # 否则调用方不播,表现成"点了不播"。
        return True


    def set_media_path(self,name:str,path:str,end_action=None):
        
        if not path or path == NO_FILE:
            self._warn_no_file_soon()
            return False
        self._stop_listen()            
        # M9:_stop_listen() 内部已经无条件地 _gen += 1(在 _listening 判断之前),
        # 这里原来又推了一次 —— 重复自增,纯冗余。
        self._set_media(path)
        self.media_key = 'music'
        self.load_message(name,path)
        if end_action is not None:
            self._listen(path,end_action)
        # 同上:封面失败不影响返回值,load_message 已经把异常吃掉。
        return True


    def load_message(self,name:str,path:"str | None"):
        """读标签/封面。**本方法保证不抛**:任何失败都退化成"没有信息+默认封面"。

        约束来自调用方:set_media_path 走完这里才会 return True,一旦这里抛出去,
        调用方就以为切歌失败而不播放,用户看到的就是"点了却不播"。
        """
        self.music_message = {'name': name}
        self._set_cover(None)
        try:
            if not path or path == NO_FILE:
                # 没有标签来源:只保留默认封面与空信息,不做无意义的标签读取
                return
            if not start_music_message:
                return
            self._read_tags(path)
        except Exception:
            logging.exception('读取标签或封面失败')
            traceback.print_exc()
            try:
                self.music_message = {'name': name}
                self._set_cover(None)
            except Exception:
                logging.exception('重置音乐信息失败(忽略)')

    def _read_tags(self,path):
        """真正的标签读取。mutagen 的对象一律 try/finally 显式关闭:
        Windows 上不关就锁着文件,重命名/删除都会失败。
        """
        a = None
        try:
            a = mutagen.File(path,easy=True)
            if a is not None:
                for aa,b in a.items():
                    self.music_message[aa] = b
        finally:
            # mutagen.File 返回的对象也可能持有文件句柄,别赌它有 __del__ 会关
            _close_quietly(a,'mutagen.File')
        _,b = os.path.splitext(path)
        b:str
        if b.lower() == '.flac':
            audio = None
            try:
                audio = mutagen.flac.FLAC(path)
                for pic in audio.pictures:
                    self.music_message['pic_mine'] = pic.mime
                    self._set_cover(pic.data)
            finally:
                _close_quietly(audio,'mutagen.flac.FLAC')
        elif b.lower() == '.mp3':
            tags = None
            try:
                tags = mutagen.id3.ID3(path)
                for pic in tags.getall("APIC"):
                    self.music_message['pic_mine'] = pic.mime
                    self._set_cover(pic.data)
            finally:
                _close_quietly(tags,'mutagen.id3.ID3')
    def play(self) -> int:
        self._set_status('播放')
        return self.music_player.play()
    def pause(self) -> None:
        self._set_status('暂停')
        self.music_player.pause()
    def stop(self) -> None:
        self._set_status('无')
        self._stop_listen()
        self.music_player.stop()
    def cleanup(self):
        # 先把 _released 置位再动 release():SeekBar 的轮询护栏靠它判断
        # "MediaPlayer 已经不能碰了"。顺序反过来的话,release() 和护栏之间
        # 就有一个窗口期,那时 _tick 仍然会去调 libvlc —— 进程级崩溃。
        # 两个对象上都要置:SeekBar 拿到的是 Tkapp 传进去的 music_player
        # (vlc.MediaPlayer),不是这里的 Player 包装对象。
        self._released = True
        try:
            self.music_player._released = True
        except Exception:
            logging.exception('在 MediaPlayer 上标记已释放失败(忽略)')
        sb = self.seekbar
        if sb is not None:
            # 显式停掉进度条轮询,不再等 <Destroy> 事件(那时 Tk 树可能已经散了)
            try:
                sb.stop_poll()
            except Exception:
                logging.exception('停止进度条轮询失败(忽略)')
            self.seekbar = None
        self._stop_listen()
        try:
            self.music_player.stop()
        except Exception:
            logging.exception('停止播放失败(忽略)')
        self._release_media()
        self.music_photo = None
        # B7:同文件里其它收尾动作(stop / stop_poll / _release_media)都各自包了
        # try/except,只有这两处 release 是裸的。而 cleanup_and_exit 的
        # sys.exit(0) 在 finally **之后**:release 一抛异常,退出码那一句就被
        # 跳过,窗口已销毁、托盘已停,进程却可能不走。
        try:
            self.music_player.release()
        except Exception:
            logging.exception('释放 MediaPlayer 失败(忽略)')
        try:
            self.player.release()
        except Exception:
            logging.exception('释放 vlc.Instance 失败(忽略)')
    def time_add_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):
            t = self.music_player.get_time()+10000
            length = self.music_player.get_length()
            
            if length > 0:
                t = min(t,length)
            self.music_player.set_time(t)
    def time_minus_ten(self) -> None:
        if self.music_player.get_state() in (vlc.State.Playing,vlc.State.Paused):self.music_player.set_time(max(self.music_player.get_time()-10000,0))
def fmt_time(ms):
    
    s = max(int(ms),0) // 1000
    h,s = divmod(s,3600)
    m,s = divmod(s,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

# env_box 现在来自 plugin_sandbox.py:每个 env_id 一个沙盒,命名空间里的
# os/sys/io/socket/... 都是受控门面,默认只允许读插件自己的目录。


    


class SeekBar(ttkbootstrap.Frame):
    SCALE_MAX = 1000        
    def __init__(self, master, media_player: "vlc.MediaPlayer",
                 interval: int = 500, owner=None, **kw):
        super().__init__(master, **kw)
        self.media_player = media_player
        self.interval = interval
        self.owner = owner          

        self._dragging = False
        self._after_id = None
        self._destroyed = False
        self._total = 0

        self.scale = ttkbootstrap.Scale(self, from_=0, to=self.SCALE_MAX,
                               orient="horizontal")
        self.scale.pack(side="left", fill="x", expand=True)

        self.label = ttkbootstrap.Label(self, text="00:00 / 00:00",
                               width=13, anchor="e")
        self.label.pack(side="right", padx=(6, 0))

        
        self.scale.bind("<ButtonPress-1>", self._on_press)
        self.scale.bind("<ButtonRelease-1>", self._on_release)
        self.scale.configure(command=self._on_scale_move)

        
        self.bind("<Destroy>",self._on_destroy)
        self._start_poll()

    def _on_destroy(self,event):
        if event.widget is not self:
            return
        self.stop_poll()

    def stop_poll(self):
        """停掉轮询。幂等,可重复调用。

        置 _destroyed 之后 _tick/_poll 都不再排期;after_cancel 失败(控件已散、
        解释器在收尾)只记日志,不能把调用方带挂 —— 退出流程里这是收尾动作。
        """
        self._destroyed = True
        aid,self._after_id = self._after_id,None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                logging.exception('取消进度条轮询失败(忽略)')

    
    def _on_press(self, _event):
        self._dragging = True

    def _on_release(self, _event):
        self._dragging = False
        # B13:与 _tick/_poll 同一道硬护栏 —— MediaPlayer/Instance 一旦 release,
        # 再调 libvlc 就是进程级崩溃(不是能 catch 的 Python 异常)。关窗瞬间
        # 松开进度条正好能踩到这个窗口。
        if self._destroyed or getattr(self.media_player,'_released',False):
            return
        if self._total > 0:
            ratio = self.scale.get() / self.SCALE_MAX
            self.media_player.set_time(int(self._total * ratio))
            
            
            self.owner.play_jump_not_stop()

    def _on_scale_move(self, value):
        
        if not self._dragging or self._total <= 0:
            return
        ratio = float(value) / self.SCALE_MAX
        cur = int(self._total * ratio)
        self.label.config(text=f"{fmt_time(cur)} / {fmt_time(self._total)}")

    
    def _start_poll(self):
        self._poll()

    def _poll(self):
        if self._destroyed or getattr(self.media_player,'_released',False):
            return
        try:
            self._tick()
        except tkinter.TclError:
            logging.exception('刷新进度条失败')
            self._destroyed = True
            self._after_id = None
            return
        try:
            self._after_id = self.after(self.interval, self._poll)
        except tkinter.TclError:
            logging.exception('重新调度进度条刷新失败')
            self._destroyed = True
            self._after_id = None

    def _tick(self):
        # 硬护栏:MediaPlayer/Instance 一旦 release,再调 libvlc 就是进程级崩溃
        # (不是能 catch 的 Python 异常)。这里直接 return 且**不重新排期** ——
        # 排期会把已经死掉的 player 一直问下去。
        if self._destroyed or getattr(self.media_player,'_released',False):
            return
        mp = self.media_player
        if self._dragging:
            return
        self._total = mp.get_length() or 0
        cur = mp.get_time() or 0

        if self._total > 0:
            self.scale.set(cur / self._total * self.SCALE_MAX)
            self.label.config(text=f"{fmt_time(cur)} / {fmt_time(self._total)}")
        else:
            self.label.config(text="00:00 / 00:00")


class Plugin:
    def __init__(self,dir:str,env_dict:env_box,other_names=None):
        self.env_dict = env_dict
        self.plugin_file_path = os.path.join(dir,'plugin.json')
        n = {}
        try:
            with open(self.plugin_file_path,'r',encoding='utf-8') as fp:
                n = json.load(fp)
        except Exception as e:
            logging.exception('读取插件配置失败')
            print(f'读取 {self.plugin_file_path} 失败,按空配置处理:{e}')
            traceback.print_exc()
        if not isinstance(n,dict):
            print(f'{self.plugin_file_path} 的内容不是 JSON 对象,按空配置处理')
            n = {}
        
        self.init = n.get('init','').replace('from b import *','',1)
        init_file = n.get('init_file','')
        if init_file:
            p = _plugin_data_path(dir,init_file,'init_file')
            if p is not None:
                try:
                    with open(p,'r',encoding="utf-8") as fp:
                        self.init = fp.read().replace('from b import *','',1)
                except Exception as e:
                    logging.exception('读取插件 init 文件失败')
                    print(f'读取 {init_file} 失败,忽略:{e}')

        self.command = n.get('command','').replace('from b import *','',1)
        command_file = n.get('command_file','')
        if command_file:
            p = _plugin_data_path(dir,command_file,'command_file')
            if p is not None:
                try:
                    with open(p,'r',encoding="utf-8") as fp:
                        self.command = fp.read().replace('from b import *','',1)
                except Exception as e:
                    logging.exception('读取插件 command 文件失败')
                    print(f'读取 {command_file} 失败,忽略:{e}')

        self.init_ok = False
        
        self.com = None
        self._box = None                 # init_env() 里由沙盒注册表给出

        name = n.get('name','')
        if isinstance(name,str) and name:
            base = name
            named = True
        else:
            # 没有 name 的插件以前会拿到空 env_id,和别的无名插件共用一份命名空间
            base = os.path.basename(os.path.normpath(dir))
            named = False
        # other_names 是"已经被别的插件占用的名字"(不含自己):重名时依次试
        # name (2)、name (3)……,保证同名插件不会共用一份 env 命名空间
        # (env_id 默认就等于 name)。
        # 注意不能用 set + discard(自己) 的写法:set 会把两个同名声明合并成
        # 一个,再把自己 discard 掉,恰好把"重名"这个证据扔了,结果两个插件
        # 都叫同一个名字 —— 那正是这里要防的事。
        others = set(other_names or ())
        self.name = base
        i = 2
        while self.name in others:
            self.name = f'{base} ({i})'
            i += 1
        if not named:
            print(f'{self.plugin_file_path} 没有可用的 name,改用目录名 {self.name!r}')
        elif self.name != base:
            print(f'{self.plugin_file_path} 的 name {base!r} 已被别的插件占用,'
                  f'改用 {self.name!r}')
        self.can_exec = n.get('can_exec',False)
        self.exec_path = n.get('exec_path',[])
        if not isinstance(self.exec_path,(list,tuple)):
            print(f'插件 {self.name} 的 exec_path 不是数组,按空处理')
            self.exec_path = []
        
        
        self.privilege = n.get('privilege',[])
        if not isinstance(self.privilege,(list,tuple)):
            print(f'插件 {self.name} 的 privilege 不是数组,按空处理')
            self.privilege = []
        
        self.dir = dir
        # 插件身份 = 插件目录的规范化 realpath(Windows 下 normcase 折成小写)。
        # 授权/白名单一律用 identity 当键:name 是插件在 plugin.json 里自报的,
        # 改个名字就能冒领别人的授权,也能靠重名撞上别人的白名单。
        self.identity = os.path.normcase(os.path.realpath(dir))
        # 沙盒策略:plugin.json 的 sandbox 段 + 历史 privilege。
        # 解析只做校验:字段写错只会更严,不会变成"不受限制"。
        self.sandbox_policy = parse_policy(n.get('sandbox',None),self.privilege,dir)
        # unsafe 只是插件"申请",要不要放开由宿主在装载时问用户(见 _confirm_unsafe)
        for w in self.sandbox_policy.warnings:
            print(f'[沙盒] {self.plugin_file_path}:{w}')

        self.n = n

        # 默认 env_id 就是插件身份,不听自报的 env_id:否则两个目录不同的插件
        # 只要自报同一个 env_id 就能共用一个沙盒命名空间(H2)。
        # 只有显式声明 "share_env": true 的插件,才按自报 env_id(缺省 'shared')
        # 去共用同一个沙盒(见 env_box.create 的 share 参数)。
        self.share_env = _plugin_share_env(n,self.name)
        self.env_id = _shared_env_key(n) if self.share_env else self.identity
    def init_env(self):

        # 命名空间由沙盒提供:默认只给"读插件自己的目录",写/网络/进程都要授权。
        # 建沙盒要带宿主凭据:插件自己调 create 是改不动策略的
        self._box = self.env_dict.create(self.env_id,self.name,self.dir,
                                         self.sandbox_policy,
                                         _host_token=_HOST_TOKEN,
                                         share=self.share_env)
        self._env_dict = self._box.namespace
        self._env_dict['__start_sandbox__'] = start_sandbox

        
    def init_i(self,tkaapp):
        self._box.attach_host(tkaapp)
        
        if not self.can_exec or self.init_ok:
            return
        
            
        try:
            
            # 先编译:语法错误在这里就能拿到,不用等回调里再炸
            code = compile(self.init,f'<plugin {self.name} init>','exec')
        except Exception as e:
            logging.exception('编译插件 init 代码失败')
            print(f'插件 {self.name} 的 init 代码无法编译,已跳过:{e}')
            return
        # L8:同 Plugin_no_sandbox —— init_ok 必须在编译成功之后才置位,
        # 否则编译失败的插件照样会去执行 command 代码。
        self.init_ok = True
        def _run_init():
            # 插件代码出问题只应该影响它自己,不能把整个播放器带崩
            try:

                exec(code,self._env_dict)
            except SandboxDenied as e:
                # 被沙盒挡下属于"预期内"的结果:一行干净提示,不吓人
                logging.warning('插件 %s 的 init 被沙盒拦截:%s',self.name,e)
                print(f'[沙盒] 插件 {self.name} 的 init 被拦截:{e}')
            except Exception as e:
                logging.exception('执行插件 init 代码失败')
                print(f'插件 {self.name} 初始化失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_init)


    def run(self,tkaapp=None):
        if not self.can_exec or tkaapp is None:
            return
        # L8 的同一条:init 没起来就不该执行 command,否则 command 会在一个
        # init 从未执行过的命名空间里 exec(通常直接 NameError)。
        if not self.init_ok:
            return
        self._box.attach_host(tkaapp)
        if not self.com:
            try:
                
                self.com = compile(self.command,f'<plugin {self.name} command>','exec')
            except Exception as e:
                logging.exception('编译插件 command 代码失败')
                print(f'插件 {self.name} 的 command 无法编译,已跳过:{e}')
                return
        
        
        
        def _run_command():
            try:
                exec(self.com,self._env_dict)
            except SandboxDenied as e:
                logging.warning('插件 %s 的 command 被沙盒拦截:%s',self.name,e)
                print(f'[沙盒] 插件 {self.name} 的 command 被拦截:{e}')
            except Exception as e:
                logging.exception('执行插件 command 代码失败')
                print(f'插件 {self.name} 执行失败:{e}')
                traceback.print_exc()
        tkaapp.app.after(0,_run_command)






class Tkapp:
    def __init__(self):
        
        self.theme_var = None
        self.hwnd = None
        # <Configure> 去抖用的 after id,以及封面 PhotoImage 的缓存键
        # (避免窗口里任何一点尺寸变化都重建一次封面图)
        self._resize_after = None
        self._cover_src = None
        self._cover_size = -1
        self.music_dict = {}
        self.player = Player(self.music_dict)
        self.index: "int | None" = None     
        self.app= ttkbootstrap.Tk('music')   
        
        # 这里原先有一段"Python < 3.10 就提示升级"的检查,但本文件用了 match 语句,
        # 低版本解释器在解析阶段就会 SyntaxError,那段代码永远执行不到,故删除。
        # 结论:本程序需要 Python >= 3.10,只能由启动方式/文档来保证。
        self.app.bind_class('Button',"<space>",self.play_stop)
        self.app.protocol('WM_DELETE_WINDOW',self.on_closing)
        self.app.title('music')
        self.app.geometry("800x600+20+200")
        self.app.rowconfigure(0, weight=10)
        self.app.rowconfigure(1, weight=1)
        self.app.rowconfigure(2, weight=10)
        self.app.columnconfigure(0, weight=3)
        self.app.columnconfigure(1, weight=1)
        self.config = Config()

        self.style = ttkbootstrap.Style()
        names = self.style.theme_names()
        theme = self.config.theme if self.config.theme in names else 'solarized-light'
        if theme in names:
            self.style.theme_use(theme)
        else:
            print(f'没有可用的主题:{theme}')
        try:
            self.raw_image_obj  = Image.open(os.path.join(BASE_DIR,'a.png'))
            # B14:显式解码一次,别把坏图留到后面(托盘 run / 控件 PhotoImage)
            self.raw_image_obj.load()
            self._tk_image_obj = ImageTk.PhotoImage(self.raw_image_obj.resize((16,16)))
            self.app.iconphoto(True,self._tk_image_obj)
        except Exception:
            # a.png 缺失/损坏不该让程序起不来:窗口图标换成 ttkbootstrap 自带
            # 默认图标,托盘也走同一条(与 _default_photo 的容错口径一致)。
            logging.exception('加载 a.png 失败,改用默认图标')
            print(f'加载 {os.path.join(BASE_DIR,"a.png")} 失败,改用默认图标')
            self.raw_image_obj = os.path.join(BASE_DIR,'a.png')
            self._tk_image_obj = None
            # 连默认图标也拿不到时直接不放,别为了一个图标把启动流程带挂
            try:
                self.app.iconphoto(True,ttkbootstrap.PhotoImage(
                    name='default', file=ttkbootstrap.ICON, subsample=8))
            except Exception:
                logging.exception('设置默认窗口图标失败(忽略)')

        
        
        self._ui_queue: "queue.Queue" = queue.Queue()
        self.icon_menu =  pystray.Menu(pystray.MenuItem('显示主界面',lambda:self._ui_queue.put('show'),default=True),
                                       pystray.MenuItem('播放',lambda:self._ui_queue.put('play')),
                                       pystray.MenuItem('暂停',lambda:self._ui_queue.put('pause')),
                                       pystray.MenuItem('退出',lambda:self._ui_queue.put('quit'))
                                       )
        self.backround_icon = pystray.Icon('music',self.raw_image_obj,'music player',self.icon_menu)
        self.music_player_icon = Thread(target=self.backround_icon.run,daemon=True)
        self.music_player_icon.start()
        self._pump_ui()

        
        self.information_frame = ttkbootstrap.Frame(self.app)
        self.information_frame.grid(row=0,column=0,sticky='nsew')
        
        self.Control_frame = ttkbootstrap.Frame(self.app)
        self.Control_frame.grid(row=0,column=1)

        self.volume_var = tkinter.IntVar(value=100)
        self.music_label = ttkbootstrap.Label(self.Control_frame,text='音量:100')
        self.music_volume = ttkbootstrap.Scale(self.Control_frame,from_=0,to=100,value=100,variable=self.volume_var,command=self.set_volume)

        self.music_label.pack()
        self.music_volume.pack()
        self.play_status_label = ttkbootstrap.Label(self.Control_frame,text="无")
        self.play_status_label.pack()

        self.play_button = ttkbootstrap.Button(self.Control_frame,text='播放',command=self.play)
        self.pause_button = ttkbootstrap.Button(self.Control_frame,text='暂停',command=self.pause)
        self.play_button.pack(side='top')
        self.pause_button.pack(side='top')
        self.player_mode_var = tkinter.StringVar(value='单曲循环')
        self.music_mode_combobox_obj = ttkbootstrap.Combobox(self.Control_frame,height=3,width=10,values=['单曲循环','顺序播放','随机播放'],textvariable=self.player_mode_var,state='readonly')
        self.music_mode_combobox_obj.pack(side='bottom')
        self.music_mode_combobox_obj.bind("<<ComboboxSelected>>",self.change_mode)

        self.Progress_bar = ttkbootstrap.Frame(self.app)
        self.Progress_bar.grid(row=1,column=0,columnspan=2,sticky='ew',padx=8,pady=4)
        self.Progress_bar.columnconfigure([0,2],weight=1)
        self.Progress_bar.columnconfigure(1,weight=10)

        self.seek = SeekBar(self.Progress_bar,self.player.music_player,owner=self.player)
        self.seek.grid(row=0,column=1,sticky='ew')
        # 回填给 Player:cleanup() 没有别的途径拿到这个进度条,而它必须在
        # release() 之前把 500ms 的轮询停掉(见 Player.cleanup / SeekBar._tick)。
        self.player.seekbar = self.seek
        self.music_minus_ten_button = ttkbootstrap.Button(self.Progress_bar,text='⏪',command=self.player.time_minus_ten)
        self.music_minus_ten_button.grid(row=0,column=0)
        self.music_add_ten_button = ttkbootstrap.Button(self.Progress_bar,text='⏩️',command=self.player.time_add_ten)
        self.music_add_ten_button.grid(row=0,column=2)

        self.information_frame.rowconfigure([0,1],weight=1)
        self.information_frame.columnconfigure([0,1],weight=1)

        # _default_photo() 承诺"封面不可用时返回 None,绝不抛"(a.png 缺失或损坏),
        # 但这里原来直接 .resize():None.resize 会让 Tkapp.__init__ 在启动路径上抛
        # AttributeError,表现成"程序起不来"。封面拿不到就按无图处理。
        ph = None
        _ph_src = _default_photo()
        if _ph_src is not None:
            try:
                ph = ImageTk.PhotoImage(_ph_src.resize((int(600/4),int(600/4))))
            except Exception:
                logging.exception('默认封面缩放失败,本次按无封面处理')
                ph = None
        self.music_image = ttkbootstrap.Label(self.information_frame,image=ph)
        self.music_keep_not_clean = ph
        self.music_name = ttkbootstrap.Label(self.information_frame)
        self.music_maker_name = ttkbootstrap.Label(self.information_frame)

        self.music_image.grid(row=0,column=0,rowspan=2,sticky='nsew')
        self.music_name.grid(row=0,column=1,sticky='nsew')
        self.music_maker_name.grid(row=1,column=1,sticky='nsew')

        self.down_frame = ttkbootstrap.Frame(self.app)
        self.down_frame.rowconfigure(0,weight=1)
        self.down_frame.columnconfigure(0,weight=40)
        self.down_frame.columnconfigure(1,weight=1)
        self.down_frame.grid(row=2,column=0,columnspan=2,sticky='nsew')
        
        self.scrollbar = ttkbootstrap.Scrollbar(self.down_frame)
        self.down_frame_listbox = ttkbootstrap.Listbox(self.down_frame,selectmode=tkinter.SINGLE,exportselection=False,yscrollcommand=self.scrollbar.set)#SINGLE 无法被键盘控制
        self.scrollbar.config(command=self.down_frame_listbox.yview)
        self.down_frame_listbox.grid(row=0,column=0,sticky='nsew')
        self.app.bind(sequence="<space>",func=self.play_stop)
        self.down_frame_listbox.bind(sequence="<space>",func=self.play_stop)
        self.menu = ttkbootstrap.Menu(self.app)


        
        self.music_mode_combobox_obj.bind('<Down>',self.volume_down)
        self.app.bind('<Down>',self.volume_down)
        self.app.bind('<Up>',self.volume_up)
        self.app.bind('<Left>',self.time_minus_ten)
        self.app.bind('<Right>',self.time_add_ten)
        self.app.bind("<Configure>", self.on_resize)
        if start_plugin and start_sandbox:
            self.env_dict = env_box()


        self.menu.add_command(label='重加载音乐列表',command=self.flush_music)
        self.menu.add_command(label='插件管理',command=self.plugin_setting)
        self.menu.add_command(label='设置',command=self.setting)

        self.env__dict = dict()
        
        self.app.config(menu=self.menu)

        self.music_mode = True
        
        self.menu.add_command(label='mv',command=self.mv_play)
        self.menu.add_command(label='music',command=self.back_music)
        self.menu.add_separator()
        self.screen = ttkbootstrap.Canvas(self.app)

        self.plugin_list = []
        self.run_play_list = []
        self.run_pause_list = []
        self.run_listbox_list = []

        plugin_dir = os.path.join(BASE_DIR,'plugin')
        # 插件按目录名排序后依次装载,assigned_names 记的是"已经分配给别的
        # 插件的名字"。Plugin 用它给自己声明的 name 消歧:同名插件会被改名,
        # 于是不会共用一份 env 命名空间(env_id 默认就等于 name)。
        # 排序只是为了让改名结果稳定、可复现。
        assigned_names = set()
        if start_plugin and start_sandbox:
            
            print('[沙盒] 插件沙盒已启用:默认只允许插件读自己的目录,'
                '写/网络/外部进程需要授权')
            print('\033[91m[warning]只能防老实的插件\n[warning]陌生插件还是发给gpt吧\033[0m',file=sys.stderr)

        # 装载的真实逻辑已经提到方法上(_load_no_sandbox / _load_sandboxed),
        # 这样「插件管理」窗口在运行期也能用同一套流程加载插件 —— 尤其是
        # 沙盒"先恢复授权、再封存"的顺序,只该有一份实现。
        def load_q(n:Plugin_no_sandbox):
            self._load_no_sandbox(n)
        def load_p(n:Plugin):
            self._load_sandboxed(n)
        self.scrollbar.grid(row=0,column=1,sticky='n')
        self.down_frame_listbox.bind('<<ListboxSelect>>', self.change_music)
        if os.path.isdir(plugin_dir) and start_plugin:
            if not start_sandbox:
                # M11:无沙盒是彻底的信任降级。原先改这个开关既不提示、也不要求
                # 重启,插件还能自己调 pro.config.save() 把降级固化下来。现在每次
                # 以无沙盒模式启动都显著告知一次。
                msg = ('沙盒已关闭:插件代码将直接运行在宿主环境中,'
                       '不会被判权,也不会再有任何权限询问。')
                logging.warning(msg)
                print('[警告] ' + msg)
                ttkbootstrap.Messagebox.show_warning(
                    msg + '\n\n如需恢复保护,请在插件设置里重新勾选"启用沙盒"并重启程序。',
                    parent=self.app)
            for asa in sorted(os.listdir(plugin_dir)):
                mn = os.path.join(plugin_dir,asa)
                if not os.path.isdir(mn):continue
                if not os.path.isfile(os.path.join(mn,'plugin.json')):continue
                n = None
                try:
                    print(mn)
                    if start_sandbox and mn not in self.config.close_safe_really_plugin:
                        n = Plugin(mn,self.env_dict,assigned_names)
                    else: n = Plugin_no_sandbox(mn)
                    
                    assigned_names.add(n.name)
                    if n.identity in self.config.allow_plugin:
                        # 沙盒策略在装载期一次定好(见 plugin_sandbox),插件自己改不了
                        if start_sandbox and mn not in self.config.close_safe_really_plugin:
                            load_p(n)
                        else:
                            print(f'no sandbox load:{n.name}')
                            load_q(n)
                    else:
                        if start_sandbox and mn not in self.config.close_safe_really_plugin:
                            ask = (f'是否加载 {n.name}\n\n它申请的权限:\n'
                               f'{n.sandbox_policy.describe()}')
                        else:ask =f'是否加载 {n.name}'

                        if ttkbootstrap.Messagebox.yesno(ask,title='插件',
                                                         parent=self.app,buttons=['是','否']) == '是':
                            if ttkbootstrap.Messagebox.yesno('是否默认加载',title='插件',
                                                             parent=self.app,buttons=['是','否']) == '是':
                                self.config.allow_plugin.append(n.identity)
                            if start_sandbox:load_p(n)
                            else:load_q(n)
                except Exception as e:
                    logging.exception('加载插件失败')
                    # 单个插件出问题不该把整个播放器带崩
                    who = n.name if n is not None else mn
                    print(f'加载插件 {who} 失败,已跳过:{e}')
                    traceback.print_exc()

        # 装载到此结束。插件的 init/command 都是 app.after 投递的,要等 mainloop
        # 才开始跑,所以上面这些装载逻辑一定先于任何插件代码执行。
        # 封存沙盒:策略从此冻结,连宿主都不能再改,插件更不可能给自己加权限。
        if start_plugin and start_sandbox:
            self.env_dict.seal()




        self.config.save()
        music_dir = os.path.join(BASE_DIR,'music')
        if not os.path.isdir(music_dir):
            
            os.makedirs(music_dir,exist_ok=True)
        self.flush_music()
    def on_resize(self,event):
        # 注意:子控件的 <Configure> 也会冒到 toplevel 的 bindtags 上,
        # 不判断 event.widget 的话,标签文字变一下就会触发一次封面缩放。
        if event.widget is not self.app:
            return
        if self._resize_after is not None:
            try:
                self.app.after_cancel(self._resize_after)
            except tkinter.TclError:
                
                pass
        self._resize_after = self.app.after(120,self._do_resize)

    def _do_resize(self):
        self._resize_after = None
        self.flush_display()

    def _select_name(self,name):
        # 让列表高亮当前正在播放的条目
        # (程序化 selection_set 不会触发 <<ListboxSelect>>,所以不会弹确认框)
        keys = list(self.music_dict.keys())
        if name not in keys:
            self.index = None
            return
        self.index = keys.index(name)
        self.down_frame_listbox.selection_clear(0,tkinter.END)
        self.down_frame_listbox.selection_set(self.index)
        self.down_frame_listbox.see(self.index)
    def cleanup_and_exit(self):
        # M13:幂等 —— 托盘退出和 WM_DELETE_WINDOW 两条路都会走到这里。
        # 没有这道闸,重复进入会把 destroy()/release() 跑第二遍,而对着已经释放的
        # MediaPlayer 再动手是进程级崩溃(不是可捕获的 Python 异常)。
        if getattr(self,'_cleaned',False):
            return
        self._cleaned = True
        # 顺序很重要:先 cleanup 再 destroy。
        # 原来是 destroy 在前 —— destroy 会拆掉 Tk 树、触发 SeekBar 的 <Destroy>,
        # 但在那之后到 player.cleanup() 真正 release() 之间,500ms 的轮询仍可能
        # 被事件循环再跑一次,那时去碰 MediaPlayer 就是进程级崩溃。
        # 反过来先 cleanup:它自己会置 _released 并显式 stop_poll(),窗口期消失。
        try:
            self.player.cleanup()
        finally:
            # M13:收尾动作彼此独立,一个失败不能拖住另一个,否则窗口关不掉、
            # 托盘图标残留、进程退不干净。
            for what,fn in (('销毁主窗口',self.app.destroy),
                            ('停止托盘图标',self.backround_icon.stop)):
                try:
                    fn()
                except Exception:
                    logging.exception(f'{what}失败(忽略)')
        sys.exit(0)

    def play_stop(self,e):
        n = self.player.music_player.get_state()
        if n == vlc.State.Playing:
            self.pause()
        elif n == vlc.State.Paused:
            self.play()
        return 'break'
        

    def volume_down(self,e):

        n = self.volume_var.get()
        if n-10<=0:self.volume_var.set(0);self.player.set_volume(0);self.music_label.config(text=f"音量:0")
        else:self.volume_var.set(n-10);self.player.set_volume(n-10);self.music_label.config(text=f"音量:{n-10}")

        return 'break'

    def volume_up(self,e):

        n = self.volume_var.get()
        if n+10>=100:self.volume_var.set(100);self.player.set_volume(100);self.music_label.config(text=f"音量:100")
        else:self.volume_var.set(n+10);self.player.set_volume(n+10);self.music_label.config(text=f"音量:{n+10}")

        return 'break'
    # ---------------- 运行期加载插件 ----------------

    def _bind_plugin_exec(self,n):
        """按 exec_path 把插件的 run() 接到播放/暂停/列表刷新上。"""
        def _bind(fn,_app=self):
            return functools.partial(fn,_app)
        if 'play' in n.exec_path:
            self.run_play_list.append(_bind(n.run))
        if 'pause' in n.exec_path:
            self.run_pause_list.append(_bind(n.run))
        if 'listbox' in n.exec_path:
            self.run_listbox_list.append(_bind(n.run))

    def _load_no_sandbox(self,n):
        """无沙盒装载(原 __init__ 里的 load_q)。"""
        n.init_env(self.env__dict)
        n.init_i(self)
        self.plugin_list.append(n)
        self._bind_plugin_exec(n)
        return True

    def _load_sandboxed(self,n,seal=False):
        """沙盒装载(原 __init__ 里的 load_p);装载期和运行期加载都走这一条。

        0) 先建沙盒:身份登记是 create() 内部完成的,identity_ok 这个结论得先在
           _box 上存在,下面 _confirm_identity 才读得到。原来这两行是反的 ——
           _confirm_identity 读 n._box.identity_ok 时 _box 还是 __init__ 里那个
           None,于是每个走沙盒的插件都抛 AttributeError。次序换了并不削弱
           fail-closed:此刻插件代码一行都还没执行,登记失败照样在下一行被挡下。

        seal 只该由**运行期加载**传 True。装载期一律 False —— 那时候宿主会在
        所有插件装完之后统一 env_dict.seal(),而声明了 share_env 的插件是**共用**
        同一个沙盒的:在这里提前封存,同 env_id 的后来者一进门就撞上"已封存,
        不能再改 _ask"(plugin/fs 和 plugin/x 都是 env_id=1,正好踩着这条)。
        """
        n.init_env()
        # share_env 的插件复用别人建好的沙盒。装载期它一定还没封存(封存是全部
        # 装完才统一做的);运行期再加载时那个沙盒早被封上了,这里不可能再往它
        # 上面挂回调。这不是越权,只是运行期装不进来 —— 给一句能看懂的话,
        # 不要把 SandboxDenied 原样抛到用户脸上。
        if n._box.is_sealed():
            print(f'[沙盒] {n.name} 要用的沙盒(env_id={n.env_id!r})已经封存,'
                  f'运行期加载不进来;重启后会随装载一起生效')
            return False
        if not self._confirm_identity(n):
            return False
        for cap,target in self.config.grants_for(n.identity):
            try:
                n._box.grant(cap,target,_host_token=_HOST_TOKEN)
            except SandboxDenied as e:
                print(f'[沙盒] 恢复 {n.name} 的授权失败,已忽略:{e}')
        for cap,target in self.config.denies_for(n.identity):
            try:
                n._box.remember_denied(cap,target,_host_token=_HOST_TOKEN)
            except SandboxDenied as e:
                print(f'[沙盒] 恢复 {n.name} 的"不再询问"失败,已忽略:{e}')
        self._confirm_unsafe(n)
        self._confirm_modules(n)
        n._box.set_ask(self._plugin_ask)
        n._box.set_persist(functools.partial(self._plugin_persist,n))
        n._box.set_persist_deny(functools.partial(self._plugin_persist_deny,n))
        n.init_i(self)
        if seal:
            try:
                n._box.seal()
            except Exception:
                logging.exception('运行期加载后封存沙盒失败')
        self.plugin_list.append(n)
        self._bind_plugin_exec(n)
        return True

    def _load_plugin_now(self,p):
        """把 _scan_plugin_dirs() 扫出来的一项真正装进当前进程。

        返回 (是否装上, 给用户看的一句说明)。
        """
        if p['loaded'] is not None:
            return False,'它已经在跑了'
        if not self.config.start_plugin:
            return False,'"启用插件"关着,加载了也不会执行'
        sandboxed = (bool(self.config.start_sandbox)
                     and p['dir'] not in self.config.close_safe_really_plugin)
        try:
            if sandboxed:
                n = Plugin(p['dir'],self.env_dict,
                           {q.name for q in self.plugin_list})
                if not self._load_sandboxed(n,seal=True):
                    return False,'装载流程拒绝了它(原因见控制台/日志里的 [沙盒] 那一行)'
            else:
                n = Plugin_no_sandbox(p['dir'])
                self._load_no_sandbox(n)
        except Exception as e:
            logging.exception('运行期加载插件失败')
            return False,f'{type(e).__name__}: {e}'
        return True,('已装载(沙盒模式)' if sandboxed else '已装载(无沙盒模式)')

    @staticmethod
    def _safe_plugin_dirname(name):
        """zip 里的顶层目录名能不能直接拿来当插件目录名。"""
        if not name or name in ('.','..') or len(name) > 64:
            return False
        if any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name):
            return False
        return not name.endswith(('.',' '))

    @staticmethod
    def _inspect_plugin_zip(zip_path):
        """只看不写:读出插件包的结构,给用户确认用。

        格式约定与 dsh/q.zip 相同:包里是**一个顶层目录**,目录里放 plugin.json
        和它的 init 文件 —— 等价于把 plugin/<名字>/ 整个打成一个包。

        所有不安全的东西都在**解压之前**挡掉:绝对路径、能写到目标目录之外的
        .. 条目、符号链接条目、散落在多个顶层目录下的文件、解开后过大的压缩
        炸弹、缺失或非法的 plugin.json。
        返回 (info, error);info 为 None 时 error 说明原因。
        """
        max_unpack = 32 * 1024 * 1024
        try:
            zf = zipfile.ZipFile(zip_path)
        except Exception as e:
            return None,f'不是能读的 zip:{e}'
        with zf:
            entries = []
            total = 0
            for item in zf.infolist():
                name = item.filename.replace('\\','/')
                if name.endswith('/'):
                    continue
                if name.startswith('/') or ':' in name.split('/')[0]:
                    return None,f'包里有意外的绝对路径:{item.filename}'
                parts = [part for part in name.split('/') if part not in ('','.')]
                if not parts or '..' in parts:
                    return None,f'包里有会写到目标目录之外的条目:{item.filename}'
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    return None,f'包里有符号链接条目:{item.filename}'
                total += item.file_size
                if total > max_unpack:
                    return None,f'解开后超过 {max_unpack // (1024 * 1024)} MB,不装'
                entries.append(item)
            if not entries:
                return None,'这个包里没有文件'
            tops = {item.filename.replace('\\','/').split('/')[0]
                    for item in entries}
            if len(tops) != 1:
                return None,('包里必须是同一个顶层目录(与 dsh/q.zip 的格式一致),'
                             f'实际有 {len(tops)} 个:{sorted(tops)}')
            top = tops.pop()
            if not Tkapp._safe_plugin_dirname(top):
                return None,f'顶层目录名 {top!r} 不能用作插件目录名'
            manifest_name = f'{top}/plugin.json'
            if manifest_name not in zf.namelist():
                return None,f'顶层目录 {top!r} 里没有 plugin.json'
            try:
                manifest = json.loads(zf.read(manifest_name).decode('utf-8'))
            except Exception as e:
                return None,f'plugin.json 读不出来:{e}'
            if not isinstance(manifest,dict):
                return None,'plugin.json 不是 JSON 对象'
            return ({'dirname':top,'manifest':manifest,
                     'target':os.path.join(BASE_DIR,'plugin',top),
                     'count':len(entries),'bytes':total,'zip':zip_path},None)

    @staticmethod
    def _extract_plugin_zip(zip_path,target,dirname):
        """把包里的内容解到 target。调用前 _inspect_plugin_zip 已经验过结构。

        这里仍按同一套规则再过滤一遍:验过不等于可以松手 —— 写文件的这个循环
        必须自己保证每个 dest 都落在 target 里。
        """
        prefix = dirname + '/'
        try:
            os.makedirs(target,exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                for item in zf.infolist():
                    name = item.filename.replace('\\','/')
                    if name.endswith('/'):
                        continue
                    if not name.startswith(prefix):
                        return False,f'条目不在顶层目录里,已中止:{item.filename}'
                    rel = name[len(prefix):]
                    if not rel or '..' in rel.split('/'):
                        return False,f'条目路径可疑,已中止:{item.filename}'
                    dest = os.path.join(target,rel)
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent,exist_ok=True)
                    with zf.open(item) as src, open(dest,'wb') as fp:
                        while True:
                            chunk = src.read(65536)
                            if not chunk:
                                break
                            fp.write(chunk)
        except Exception as e:
            logging.exception('解压插件包失败')
            return False,f'{type(e).__name__}: {e}'
        return True,''

    # ---------------- 插件管理窗口 ----------------

    def _scan_plugin_dirs(self):
        """扫 plugin/ 下所有"有 plugin.json"的目录,不管它这次有没有被装载。

        管理界面必须以磁盘为准:没被装载的插件同样要能启用、能看权限,否则用户
        第一次想启用某个插件就只能等启动时那个弹窗。identity 用和 Plugin /
        Plugin_no_sandbox 完全相同的口径算,里面的授权才认得出来。
        """
        out = []
        loaded = {}
        for p in (getattr(self,'plugin_list',None) or []):
            ident = getattr(p,'identity',None)
            if ident:
                loaded[ident] = p
        plugin_dir = os.path.join(BASE_DIR,'plugin')
        try:
            entries = sorted(os.listdir(plugin_dir))
        except OSError:
            logging.exception('扫描 plugin 目录失败')
            return out
        for entry in entries:
            full = os.path.join(plugin_dir,entry)
            if not os.path.isdir(full):
                continue
            manifest_path = os.path.join(full,'plugin.json')
            if not os.path.isfile(manifest_path):
                continue
            manifest = {}
            try:
                with open(manifest_path,'r',encoding='utf-8') as fp:
                    manifest = json.load(fp)
            except Exception:
                logging.exception('读取插件配置失败')
                manifest = {}
            if not isinstance(manifest,dict):
                manifest = {}
            name = manifest.get('name') or entry
            if not isinstance(name,str) or not name:
                name = entry
            identity = os.path.normcase(os.path.realpath(full))
            out.append({'dir':full,'identity':identity,'name':name,
                        'folder':entry,'manifest':manifest,
                        'loaded':loaded.get(identity)})
        return out

    @staticmethod
    def _policy_text(manifest):
        """把 plugin.json 的 sandbox 段压成一行"它申请什么",给列表当摘要列。"""
        sb = manifest.get('sandbox')
        if not isinstance(sb,dict):
            sb = {}
        legacy = manifest.get('privilege')
        legacy = legacy if isinstance(legacy,(list,tuple)) else ()
        bits = []
        rd = sb.get('fs_read')
        rd = list(rd) if isinstance(rd,(list,tuple)) and rd else ['.']
        bits.append('读:'+','.join(str(x) for x in rd))
        wr = sb.get('fs_write')
        if isinstance(wr,(list,tuple)) and wr:
            bits.append('写:'+','.join(str(x) for x in wr))
        if sb.get('net'):
            bits.append('网络')
        if sb.get('proc'):
            bits.append('进程')
        if sb.get('unsafe') or 'no_sandbox_really' in legacy:
            bits.append('完全不受限')
        mods = sb.get('modules')
        if isinstance(mods,(list,tuple)) and mods:
            bits.append('模块:'+','.join(str(x) for x in mods))
        return ' · '.join(bits)

    @staticmethod
    def _read_can_exec(manifest):
        """插件自己声明的 can_exec(缺省 False,与 Plugin.__init__ 同口径)。

        宿主在两个地方按它拦人:Plugin.init_i 和 Plugin.run 一进门就是
        `if not self.can_exec: return`。所以 can_exec=False 的插件一行代码都
        不会执行 —— 这才是"停用"该动的地方。
        """
        return bool(manifest.get('can_exec',False))

    @staticmethod
    def _write_can_exec(manifest_path,enabled):
        """只改 plugin.json 里 can_exec 这一个值,其余内容原样保留。

        刻意不用 json.load + json.dump 重写:那会把作者手写的缩进和键的顺序
        一起抹平,一个"扳开关"的动作不该动整份文件。字段不存在时只有"启用"
        需要补 —— 缺省就是 False,见 Plugin.__init__ 的 n.get('can_exec',False)。

        读写都用 newline='':不能让 Python 顺手把 CRLF 换成 LF,那也算改文件。
        返回 (配置是否真的变了, 给用户看的一句说明)。
        """
        want = 'true' if enabled else 'false'
        try:
            with open(manifest_path,'r',encoding='utf-8',newline='') as fp:
                text = fp.read()
        except OSError as e:
            return False,f'读不了 plugin.json:{e}'
        try:
            data = json.loads(text)
        except Exception as e:
            return False,f'plugin.json 不是合法 JSON,没敢改:{e}'
        if not isinstance(data,dict):
            return False,'plugin.json 不是 JSON 对象,没敢改'
        if bool(data.get('can_exec',False)) == bool(enabled):
            return False,'已经是这个状态了'

        idx = text.find('"can_exec"')
        if idx != -1:
            colon = text.find(':',idx + len('"can_exec"'))
            if colon == -1:
                return False,'plugin.json 里的 can_exec 看不懂,没敢改'
            start = colon + 1
            while start < len(text) and text[start] in ' \t':
                start += 1
            end = start
            while end < len(text) and text[end] not in ',}\r\n':
                end += 1
            if text[start:end].strip() not in ('true','false'):
                return False,'can_exec 的值不是 true/false,没敢改'
            new_text = text[:start] + want + text[end:]
        else:
            if not enabled:
                return False,'plugin.json 里没有 can_exec(缺省就是不启用),不用改'
            brace = text.find('{')
            if brace == -1:
                return False,'plugin.json 里找不到对象起始的 {,没敢改'
            new_text = text[:brace+1] + f'\n  "can_exec": {want},' + text[brace+1:]

        try:
            with open(manifest_path,'w',encoding='utf-8',newline='') as fp:
                fp.write(new_text)
        except OSError as e:
            return False,f'写不回 plugin.json:{e}'
        return True,f'can_exec 已改成 {want}'

    def plugin_setting(self):
        """插件管理:启停、看权限、逐条撤销、单个插件切沙盒。

        两个开关是分开的,别混:
          * 「启用/停用」写插件自己的 plugin.json(can_exec)—— 真开关,
            can_exec=False 的插件一行代码都不会执行;
          * 「免询问」写 config.json(allow_plugin)—— 只管启动时问不问,
            动它拦不住任何插件,点了"是"照样装上。

        两条贯穿全窗口的原则:
          * 收紧方向(停用 / 撤销授权 / 取消不受限)直接生效,不拦着用户;
          * 放宽方向(启用 / 无沙盒 / 额外模块)要二次确认,而且只写配置 ——
            正在跑的那个插件在这一进程里不会被"就地放权",一切等重启后按新的
            plugin.json / config.json 装载。沙盒在装载结束时就封存了,改不动。
        """
        ui = ttkbootstrap.Toplevel('插件管理',size=(900,640))
        ui.minsize(720,520)
        state = {'plugins':[], 'entries':[]}
        dirty = {'v':False}

        top = ttkbootstrap.Frame(ui)
        top.pack(fill='x',padx=12,pady=(10,4))
        start_plugin_var = tkinter.BooleanVar(value=self.config.start_plugin)
        start_sandbox_var = tkinter.BooleanVar(value=self.config.start_sandbox)
        ttkbootstrap.Checkbutton(top,text='启用插件',variable=start_plugin_var,
                                 onvalue=True,offvalue=False).pack(side='left')
        ttkbootstrap.Checkbutton(top,text='启用沙盒',variable=start_sandbox_var,
                                 onvalue=True,offvalue=False).pack(side='left',padx=(12,0))
        ttkbootstrap.Label(top,text='改动写入 config.json / plugin.json,重启后生效').pack(side='right')

        list_box = ttkbootstrap.Frame(ui)
        list_box.pack(fill='both',expand=True,padx=12)
        tree = ttkbootstrap.Treeview(list_box,
                                     columns=('enabled','name','status','mode','ask','policy'),
                                     show='headings',height=10)
        for key,text,width,anchor in (('enabled','启用',52,'center'),
                                      ('name','插件',150,'w'),
                                      ('status','状态',64,'center'),
                                      ('mode','运行模式',130,'w'),
                                      ('ask','免询问',62,'center'),
                                      ('policy','申请的权限',330,'w')):
            tree.heading(key,text=text)
            tree.column(key,width=width,anchor=anchor,stretch=(key=='policy'))
        tsb = ttkbootstrap.Scrollbar(list_box,orient='vertical',command=tree.yview)
        tree.configure(yscrollcommand=tsb.set)
        tsb.pack(side='right',fill='y')
        tree.pack(side='left',fill='both',expand=True)

        ttkbootstrap.Label(ui,text='选中插件的权限(在下面选一条即可撤销)',
                           anchor='w').pack(fill='x',padx=12,pady=(10,2))
        detail_box = ttkbootstrap.Frame(ui)
        detail_box.pack(fill='both',expand=True,padx=12)
        etree = ttkbootstrap.Treeview(detail_box,
                                      columns=('kind','cap','target'),
                                      show='headings',height=7)
        for key,text,width,anchor in (('kind','类型',120,'center'),
                                      ('cap','能力',120,'center'),
                                      ('target','目标',540,'w')):
            etree.heading(key,text=text)
            etree.column(key,width=width,anchor=anchor,stretch=(key=='target'))
        esb = ttkbootstrap.Scrollbar(detail_box,orient='vertical',command=etree.yview)
        etree.configure(yscrollcommand=esb.set)
        esb.pack(side='right',fill='y')
        etree.pack(side='left',fill='both',expand=True)

        status = ttkbootstrap.Label(ui,text='',anchor='w')
        status.pack(fill='x',padx=12,pady=(8,0))
        foot = ttkbootstrap.Frame(ui)
        foot.pack(fill='x',padx=12,pady=10)

        def say(text):
            status.configure(text=text)

        def selected_plugin():
            sel = tree.selection()
            if not sel:
                return None
            for p in state['plugins']:
                if p['identity'] == sel[0]:
                    return p
            return None

        def refresh_plugins(keep=None):
            if keep is None:
                keep = tree.selection()
            for item in tree.get_children():
                tree.delete(item)
            state['plugins'] = self._scan_plugin_dirs()
            for p in state['plugins']:
                view = self.config.plugin_view(p['identity'])
                mode = '无沙盒' if view['no_sandbox'] else '沙盒'
                if view['unsafe']:
                    mode += ' + 完全不受限'
                tree.insert('','end',iid=p['identity'],values=(
                    '✓' if self._read_can_exec(p['manifest']) else '',
                    p['name'],
                    '已装载' if p['loaded'] is not None else '未装载',
                    mode,
                    '✓' if view['allow'] else '',
                    self._policy_text(p['manifest'])))
            for ident in (keep or ()):
                if tree.exists(ident):
                    tree.selection_set(ident)
                    break

        def refresh_entries():
            for item in etree.get_children():
                etree.delete(item)
            state['entries'] = []
            p = selected_plugin()
            if p is None:
                return
            view = self.config.plugin_view(p['identity'])

            def add(kind,cap,target,text):
                i = str(len(state['entries']))
                state['entries'].append({'kind':kind,'cap':cap,'target':target})
                etree.insert('','end',iid=i,
                             values=(text,cap,target if target else '(整个能力)'))

            if view['unsafe']:
                add('unsafe','unsafe',None,'完全不受限')
            for cap,target in view['grants']:
                add('grant',cap,target,'已授权')
            for cap,target in view['denies']:
                add('deny',cap,target,'不再询问')
            for m in view['modules']:
                add('module','modules',m,'额外模块')

        def apply_change(changed,text):
            if not changed:
                say('这一项没有变化')
                return False
            self.config.save()
            dirty['v'] = True
            say(text + '(已写入 config.json,重启后生效)')
            return True

        def on_pick(_event=None):
            refresh_entries()
            p = selected_plugin()
            if p is not None:
                switch = '启用' if self._read_can_exec(p['manifest']) else '停用'
                ask = '免询问' if self.config.plugin_view(p['identity'])['allow'] else '每次询问'
                say(f'{p["name"]}({p["folder"]}) — {switch} · {ask} · '
                    f'{self._policy_text(p["manifest"])}')

        def toggle_enabled():
            """启用/停用 = 直接改插件自己的 can_exec。

            这才是真开关:can_exec=False 时 Plugin.init_i / run 一进门就返回,
            插件一行代码都不会跑。改 allow_plugin 只是让它下次启动再问一遍,
            用户点"是"照样装上 —— 那不叫停用,原来的实现就栽在这。
            """
            p = selected_plugin()
            if p is None:
                say('先在上面选一个插件')
                return
            want = not self._read_can_exec(p['manifest'])
            if want:
                ans = ttkbootstrap.Messagebox.yesno(
                    f'启用「{p["name"]}」?\n\n它申请的权限:\n'
                    f'{self._policy_text(p["manifest"])}\n\n'
                    '这会把插件自己的 plugin.json 里 can_exec 改成 true。',
                    '启用插件',parent=ui,buttons=['启用','取消'])
                if ans != '启用':
                    return
            changed,detail = self._write_can_exec(
                os.path.join(p['dir'],'plugin.json'),want)
            if not changed:
                say(f'没改动:{detail}')
            else:
                dirty['v'] = True
                say(f'「{p["name"]}」已{"启用" if want else "停用"}'
                    f'({detail};重启后生效)')
            refresh_plugins()
            refresh_entries()

        def toggle_allow():
            """免询问开关 = allow_plugin:在名单里,启动时不再问"是否加载"。

            它和"启用/停用"是两件事,所以这里不碰 can_exec —— 一个插件可以是
            "启用但每次都要问",也可以是"免询问但仍然不启用"。
            """
            p = selected_plugin()
            if p is None:
                say('先在上面选一个插件')
                return
            view = self.config.plugin_view(p['identity'])
            if view['allow']:
                apply_change(self.config.allow(p['identity'],False),
                             f'「{p["name"]}」已移出免询问名单(下次启动还会问)')
            else:
                apply_change(self.config.allow(p['identity'],True),
                             f'「{p["name"]}」已加入免询问名单(下次启动直接加载)')
            refresh_plugins()
            refresh_entries()

        def toggle_no_sandbox():
            p = selected_plugin()
            if p is None:
                say('先在上面选一个插件')
                return
            view = self.config.plugin_view(p['identity'])
            if view['no_sandbox']:
                apply_change(self.config.set_no_sandbox(p['identity'],False),
                             f'「{p["name"]}」已改回沙盒模式')
            else:
                ans = ttkbootstrap.Messagebox.yesno(
                    f'让「{p["name"]}」以无沙盒模式运行?\n\n'
                    '它的代码将直接跑在播放器进程里:不再判权、不再问权限,\n'
                    '和播放器本身一样可信。这比"完全不受限"更彻底 ——\n'
                    '后者至少还留着审计钩子。',
                    '信任降级',parent=ui,buttons=['我明白,继续','取消'])
                if ans != '我明白,继续':
                    return
                apply_change(self.config.set_no_sandbox(p['identity'],True),
                             f'「{p["name"]}」已标记为无沙盒运行')
            refresh_plugins()
            refresh_entries()

        def revoke():
            p = selected_plugin()
            sel = etree.selection()
            if p is None or not sel:
                say('先在下面选一条权限')
                return
            e = state['entries'][int(sel[0])]
            ident = p['identity']
            if e['kind'] == 'unsafe':
                changed = self.config.set_unsafe(ident,False)
                what = '完全不受限'
            elif e['kind'] == 'grant':
                changed = self.config.revoke(ident,e['cap'],e['target'])
                what = f'{e["cap"]} {e["target"] or ""}'
            elif e['kind'] == 'deny':
                changed = self.config.forget_deny(ident,e['cap'],e['target'])
                what = f'{e["cap"]} {e["target"] or ""}(不再询问)'
            elif e['kind'] == 'module':
                left = [m for m in (self.config.plugin_modules.get(ident) or [])
                        if m != e['target']]
                changed = self.config.set_modules(ident,left)
                what = f'额外模块 {e["target"]}'
            else:
                changed = False
                what = '未知项'
            if changed:
                apply_change(True,f'已撤销「{p["name"]}」的 {what}')
            refresh_plugins()
            refresh_entries()

        def forget_all_deny():
            p = selected_plugin()
            if p is None:
                say('先在上面选一个插件')
                return
            apply_change(self.config.forget_deny(p['identity']),
                         f'已清掉「{p["name"]}」的全部"不再询问"')
            refresh_entries()

        def _ask(message,title,buttons):
            try:
                return ttkbootstrap.Messagebox.yesno(message,title,
                                                     parent=ui,buttons=buttons)
            except Exception:
                logging.exception('确认框弹不出来,按取消处理')
                return buttons[-1]

        def _warn(title,message):
            print(f'[插件] {title}:{message}')
            try:
                ttkbootstrap.Messagebox.show_warning(message,parent=ui)
            except Exception:
                logging.exception('提示框弹不出来')

        def load_from_zip():
            """从插件包装一个进来,并立刻加载到当前进程。

            包格式与 dsh/q.zip 一致:一个顶层目录,里面是 plugin.json 和它的
            init 文件。先读后写 —— 包结构不对、路径可疑、目标已存在,统统在
            解压之前拦下,不会先落一堆文件再报错。
            """
            path = tkinter.filedialog.askopenfilename(
                title='选择插件包(zip)',parent=ui,
                filetypes=[('插件包','*.zip'),('所有文件','*.*')])
            if not path:
                return
            info,error = self._inspect_plugin_zip(path)
            if error:
                _warn('这个包不能装',error)
                say(error)
                return
            target = info['target']
            if os.path.exists(target):
                if _ask(f'插件目录已经存在:\n{target}\n\n覆盖它?',
                        '已存在',['覆盖','取消']) != '覆盖':
                    return
            name = info['manifest'].get('name') or info['dirname']
            if _ask(f'装这个插件?\n\n包:{os.path.basename(path)}\n'
                    f'插件名:{name}\n'
                    f'会解到:{target}\n'
                    f'内容:{info["count"]} 个文件,约 {max(1,info["bytes"] // 1024)} KB\n\n'
                    f'它申请的权限:\n{self._policy_text(info["manifest"])}\n\n'
                    '装好会立刻加载:里面的代码会被执行。',
                    '加载插件',['装并加载','取消']) != '装并加载':
                return
            ok,detail = self._extract_plugin_zip(path,target,info['dirname'])
            if not ok:
                _warn('解压失败',detail)
                say(detail)
                return
            refresh_plugins()
            fresh = None
            for p in state['plugins']:
                if p['dir'] == target:
                    fresh = p
                    break
            if fresh is None:
                _warn('装好了但没扫到',
                      'zip 解开后没在 plugin/ 里找到它,检查一下包结构。')
                return
            loaded,detail = self._load_plugin_now(fresh)
            refresh_plugins()
            refresh_entries()
            if tree.exists(fresh['identity']):
                tree.selection_set(fresh['identity'])
                refresh_entries()
            if loaded:
                say(f'「{fresh["name"]}」{detail}'
                    '(想让它下次启动不再问,点"免询问开关")')
            else:
                say(f'已解到 plugin/{info["dirname"]},但没加载:{detail}')

        def close():
            was = self.config.start_sandbox
            self.config.start_plugin = bool(start_plugin_var.get())
            self.config.start_sandbox = bool(start_sandbox_var.get())
            self.config.save()
            if bool(was) != bool(self.config.start_sandbox):
                if self.config.start_sandbox:
                    msg = '沙盒已开启,需要重启程序才生效。'
                else:
                    msg = ('沙盒已关闭,需要重启程序才生效。\n'
                           '关闭后插件代码将直接运行在宿主环境中,不再有任何权限限制。')
                ttkbootstrap.Messagebox.show_warning(msg,parent=ui)
            ui.destroy()

        ttkbootstrap.Button(foot,text='启用/停用',
                            command=toggle_enabled).pack(side='left')
        ttkbootstrap.Button(foot,text='免询问开关',
                            command=toggle_allow).pack(side='left',padx=6)
        ttkbootstrap.Button(foot,text='沙盒/无沙盒',
                            command=toggle_no_sandbox).pack(side='left',padx=6)
        ttkbootstrap.Button(foot,text='撤销选中权限',
                            command=revoke).pack(side='left',padx=6)
        ttkbootstrap.Button(foot,text='清空"不再询问"',
                            command=forget_all_deny).pack(side='left')
        ttkbootstrap.Button(foot,text='加载插件…',
                            command=load_from_zip).pack(side='right',padx=6)
        ttkbootstrap.Button(foot,text='关闭',command=close).pack(side='right')

        tree.bind('<<TreeviewSelect>>',on_pick)
        refresh_plugins()
        refresh_entries()

    def _pump_ui(self):

        try:
            while True:
                cmd = self._ui_queue.get_nowait()
                if cmd == 'show':
                    self.app.deiconify()
                elif cmd == 'play':
                    self.play()
                elif cmd == 'pause':
                    self.pause()
                elif cmd == 'quit':
                    self.cleanup_and_exit()
                    return              
        except queue.Empty:
            pass
        except tkinter.TclError:
            # M10:原来这里是 `return` —— 一次 TclError 就把轮询永久停掉,
            # 托盘菜单从此静默失效(不报错、不响应)。改成记日志后照常重新排期,
            # 让下一次心跳还有机会恢复。
            logging.exception('处理托盘命令时出错(已忽略,轮询继续)')
        try:
            self.app.after(100,self._pump_ui)
        except tkinter.TclError:
            logging.exception('重新排期托盘轮询失败')
    _EXIT_DIRECT = 0
    _EXIT_TRAY = 1
    _EXIT_ASK = 2

    # 这些控件自己要用方向键做导航/调值,方向键落到它们身上时不该去改播放进度。


    def time_add_ten(self,event):

        self.player.time_add_ten()
        return 'break'
    def time_minus_ten(self,event):

        self.player.time_minus_ten()
        return 'break'
    def _show_exit_dialog(self) -> int:

        r = ttkbootstrap.Messagebox.yesnocancel(
            '请选择退出方式：直接退出 / 仅关闭窗口 / 取消',
            '退出',
            parent=self.app,
            buttons=['直接退出', '仅关闭窗口', '取消'],
        )
        if r == '直接退出' or r is True:
            return self._EXIT_DIRECT
        if r == '仅关闭窗口':
            return self._EXIT_TRAY
        return -1

    def on_closing(self):
        way = self.config.exit_way
        if way == self._EXIT_ASK:
            way = self._show_exit_dialog()
        if way == self._EXIT_DIRECT:
            self.cleanup_and_exit()
        elif way == self._EXIT_TRAY:
            self.app.withdraw()


    def setting(self):
        a = ttkbootstrap.Toplevel('setting',size=(250,250))
        v = tkinter.IntVar()
        v.set(self.config.exit_way)
        ttkbootstrap.Radiobutton(a, text="直接退出", variable=v, value=0).pack(anchor="w")
        ttkbootstrap.Radiobutton(a, text="仅关闭窗口", variable=v, value=1).pack(anchor="w")
        ttkbootstrap.Radiobutton(a, text="每次询问", variable=v, value=Tkapp._EXIT_ASK).pack(anchor="w")
        self.theme_var: tkinter.StringVar = tkinter.StringVar(value=self.style.theme_use())
        na = ttkbootstrap.Combobox(a,textvariable=self.theme_var,values=self.style.theme_names(),state='readonly')
        na.pack(anchor='w')
        def save():
            self.config.exit_way = v.get()
            self.config.theme = na.get()
            self.style.theme_use(self.config.theme)
            self.config.save()
            a.destroy()
        ttkbootstrap.Button(a,text='保存',command=save).pack()

    def set_volume(self,a):
        b = round(float(a))
        self.music_label.config(text=f"音量:{b}")
        self.player.set_volume(b)
    def play(self):
        for xnn in self.run_play_list:xnn()
        
        # L9:同样是接住返回值 —— play() 起不来时返回 -1,原来完全静默。
        if self.player.play() == -1:
            ttkbootstrap.Messagebox.show_warning('播放失败:播放器无法启动',parent=self.app)
    def pause(self):
        for xnn in self.run_pause_list:xnn()
        self.player.pause()

    def mv_play(self):
        self.music_mode = False
        self.information_frame.grid_remove()
        self.player.stop_listen()
        self.screen.grid(row=0,column=0,sticky='nsew')
        if platform.system() == "Windows":
            self.hwnd = self.screen.winfo_id()
            self.player.music_player.set_hwnd(self.hwnd)
        elif platform.system() == "Linux":
            self.player.music_player.set_xwindow(self.screen.winfo_id())
        elif platform.system() == "Darwin":
            # 未在 macOS 上验证:python-vlc 这里要的是 NSView*,Tk 的 window id 未必能用
            self.player.music_player.set_nsobject(self.screen.winfo_id())
        x = self.player.music_message.get('name','')
        video = (self.music_dict.get(x) or {}).get('video',NO_FILE)
        self.player.stop()
        
        if x and video != NO_FILE:
            # 与音频模式保持一致:选中当前条目,设置好媒体之后立刻开始播放
            if self.player.set_media_path_mv(x,video,self.flush_display):
                self._select_name(x)
                self.player.start_current()
        
    def back_music(self):
        self.music_mode = True
        self.screen.grid_remove()
        self.information_frame.grid()
        x = self.player.music_message.get('name','')
        self.player.stop()
        music = (self.music_dict.get(x) or {}).get('music',NO_FILE)
        if x and music != NO_FILE:
            # 从 MV 切回音频:同样选中条目并直接播放
            if self.player.set_media_path(x,music,self.flush_display):
                self._select_name(x)
                self.player.start_current()
    def flush_display(self):
        msg = self.player.music_message
        src = self.player.music_photo
        if src is not None:
            size = max(self.app.winfo_height() // 4, 32)
            # 只有封面换了或者目标尺寸变了才重建 PhotoImage:
            # 这个方法会被切歌回调和 <Configure> 频繁触发,重建一次的代价不小。
            if src is not self._cover_src or size != self._cover_size:
                self._cover_src,self._cover_size = src,size
                self.music_keep_not_clean = ImageTk.PhotoImage(src.resize((size, size)))
                self.music_image.config(image=self.music_keep_not_clean)

        def tag_text(key):
            
            
            v = msg.get(key)
            if not v:v = ['unknow']
            if isinstance(v,str):v = [v]
            return ','.join(str(x) for x in v if x) or 'unknow'

        self.music_name.config(text=f'标题:{tag_text("title")}')
        self.music_maker_name.config(text=f'作者:{tag_text("artist")}')

    def change_mode(self,event):
        a = self.player_mode_var.get()
        match a:
            case '单曲循环':self.player.set_play_way(0)
            case '顺序播放':self.player.set_play_way(1)
            case '随机播放':self.player.set_play_way(2)
            case _:print(f'未知播放模式:{a}')


    def change_music(self,event):
        for xnn in self.run_listbox_list:xnn()
        sel = self.down_frame_listbox.curselection()
        if not sel:return      
        idx = int(sel[0])
        a = self.down_frame_listbox.get(idx)
        if self.music_mode:
            file = self.music_dict.get(a,{}).get("music",NO_FILE)

            
            if not file or file == NO_FILE:
                ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.app)
                self._restore_selection()
                return

            aaa = ttkbootstrap.Messagebox.yesno('是否切换音乐','music player',parent=self.app,buttons=['是','否'])
            if aaa == '是':
                self.player.stop()
                if self.player.set_media_path(a,file,self.flush_display):
                    self.index = idx
                    self.player.start_current()
                    self.flush_display()
                else:
                    
                    self._restore_selection()
            else:
                self._restore_selection()
        else:
            file = self.music_dict.get(a,{}).get("video",NO_FILE)
            
            
            if not file or file == NO_FILE:
                ttkbootstrap.Messagebox.show_warning('未指定文件',parent=self.app)
                self._restore_selection()
                return
            
            aaa = ttkbootstrap.Messagebox.yesno('是否切换视频','music player',parent=self.app,buttons=['是','否'])
            if aaa == '是':
                self.player.stop()
                if self.player.set_media_path_mv(a,file,self.flush_display):
                    self.index = idx
                    self.player.start_current()

                else:
                    
                    self._restore_selection()
            else:
                self._restore_selection()
        self.down_frame_listbox.focus_set()
    def _restore_selection(self):
        self.down_frame_listbox.selection_clear(0, tkinter.END)
        if self.index is not None and 0 <= self.index < self.down_frame_listbox.size():
            self.down_frame_listbox.selection_set(self.index)


    def flush_music(self):
        self.music_dict=self.load_music()
        keys = list(self.music_dict.keys())
        self.player.set_dict_and_tk_obj(self.music_dict,self.down_frame_listbox,self.play_status_label)
        self.down_frame_listbox.delete(0,tkinter.END)
        for asa in keys:
            self.down_frame_listbox.insert(tkinter.END,asa)
        
        
        self.index = None
        self.player.stop_listen()
        # 重扫之后当前这一首可能已经不在列表里了。还在的话把结束监听接回去,
        # 否则"重加载音乐列表"会让正在放的那首歌播完就停,不再自动切歌。
        if self.player.music_message.get('name','') in self.music_dict:
            self.player.rearm_listen(self.flush_display)
        self.down_frame_listbox.selection_clear(0,tkinter.END)


    # ---------------- 插件沙盒的宿主侧 ----------------
    _PLUGIN_CAP_TEXT = {'fs:read':'读取文件','fs:write':'写入文件',
                        'net':'访问网络','proc':'启动外部进程'}

    def _plugin_ask(self,box,cap,target,detail):
        """插件运行期要权限时弹窗问用户;返回值是沙盒约定的答案。"""
        what = self._PLUGIN_CAP_TEXT.get(cap,cap)
        msg = (f'插件「{box.name}」想要{what}。\n\n'
               f'目标:{target or "(整个能力)"}\n'
               f'起因:{detail or "未说明"}\n\n'
               '允许本次 / 本次运行都允许 / 总是允许(写进 config.json) / 不再询问(拒绝) / 拒绝 ')
        try:
            r = ttkbootstrap.Messagebox.yesno(msg,'插件请求权限',parent=self.app,
                                              buttons=['允许本次','本次运行都允许',
                                                       '总是允许','不再询问','拒绝'])
        except Exception:
            logging.exception('插件权限询问失败,按拒绝处理')
            return 'no'
        return {'允许本次':ASK_YES,'本次运行都允许':ASK_SESSION,
                '总是允许':ASK_ALWAYS,'不再询问':ASK_NEVER}.get(r,'no')

    def _plugin_persist(self,n,name,cap,target):
        """用户点了"总是允许":按插件身份记进 config.json,以后装载自动生效。

        注册时用 functools.partial 绑定了插件对象,沙盒仍按老约定传显示名,
        所以这里收下 name 只为兼容调用约定(写配置一律用 n.identity)。
        """
        self.config.grant(n.identity,cap,target)
        self.config.save()
        print(f'[沙盒] 已记住:{n.name} 可以使用 {cap} {target or ""}')

    def _plugin_persist_deny(self,n,name,cap,target):
        """用户点了"不再询问":按插件身份记进 config.json,以后这一类直接拒绝、不再弹窗。

        与 _plugin_persist 对称:沙盒只传显示名,写配置一律用 n.identity。
        target 是沙盒用 _grant_root() 算好后传来的,这里原样落盘(见 Config.deny)。
        """
        try:
            self.config.deny(n.identity,cap,target)
            self.config.save()
        except Exception:
            logging.exception('保存"不再询问"失败')
            return
        print(f'[沙盒] 已记住:{n.name} 以后不再询问 {cap} {target or ""}')

    def _confirm_identity(self,n):
        """插件代码身份登记失败时不可运行:身份都定不下来,授权记给谁都不对。"""
        if not n._box.identity_ok:
            print(f'[沙盒] {n.name} 的插件代码身份登记失败'
                  f'(目录 {n.dir} 无法确认为可信来源),已拒绝加载')
            return False
        return True

    def _confirm_modules(self,n):
        """插件申请额外模块:模块不受沙盒代理,只有你确认过(记在 config)才放行。"""
        if not n.sandbox_policy.modules_requested:
            return False
        if n.identity in self.config.plugin_modules:
            names = list(self.config.plugin_modules[n.identity])
            n._box.approve_modules(names,_host_token=_HOST_TOKEN)
            print(f'[沙盒] {n.name} 按你之前的确认,放行额外模块:{"、".join(names)}')
            return True
        try:
            ans = ttkbootstrap.Messagebox.yesno(
                f'插件「{n.name}」申请额外模块:{"、".join(n.sandbox_policy.modules_requested)}\n\n'
                '这些模块不受沙盒代理:插件一旦拿到它们,等于拥有完全权限,\n'
                '可以绕开沙盒读写任意文件、联网、启动进程。\n\n'
                '是否允许?(允许后会记在 config.json 的 plugin_modules 里)',
                '插件申请额外模块',parent=self.app,buttons=['是','否']) == '是'
        except Exception:
            logging.exception('插件额外模块的询问失败,按拒绝处理')
            ans = False
        if not ans:
            print(f'[沙盒] {n.name} 申请额外模块但你未同意,按拒绝处理')
            return False
        self.config.plugin_modules[n.identity] = list(n.sandbox_policy.modules_requested)
        self.config.save()
        n._box.approve_modules(self.config.plugin_modules[n.identity],
                               _host_token=_HOST_TOKEN)
        return True

    def _confirm_unsafe(self,n):
        """插件声明"不受沙盒限制":只有你确认过(记在 config)才真的放开。"""
        if not n.sandbox_policy.unsafe_requested:
            return False
        if n.identity in self.config.plugin_unsafe:
            n._box.set_unsafe(True,_host_token=_HOST_TOKEN)
            print(f'[沙盒] {n.name} 按你之前的确认,完全不受沙盒限制')
            return True
        try:
            ans = ttkbootstrap.Messagebox.yesno(
                f'插件「{n.name}」要求完全不受沙盒限制:\n'
                '它将以播放器的全部权限运行:可以读写任何文件、联网、启动进程。\n\n'
                '是否允许?(允许后会记在 config.json 的 plugin_unsafe 里)',
                '插件请求解除沙盒',parent=self.app,buttons=['是','否']) == '是'
        except Exception:
            logging.exception('插件解除沙盒的询问失败,按拒绝处理')
            ans = False
        if not ans:
            print(f'[沙盒] {n.name} 要求不受限制但你未同意,按受限处理')
            return False
        self.config.plugin_unsafe.append(n.identity)
        self.config.save()
        n._box.set_unsafe(True,_host_token=_HOST_TOKEN)
        return True

    @staticmethod
    def load_music():
        aaa = {}
        music_dir = os.path.join(BASE_DIR,'music')
        for a in sorted(os.listdir(music_dir)):
            b = os.path.join(music_dir,a)
            if not os.path.isdir(b):
                continue
            info = os.path.join(b,'info.json')
            if not os.path.isfile(info):
                continue

            try:
                with open(info,'r',encoding='utf-8') as fp:
                    n:dict = json.load(fp)
            except Exception as e:
                logging.exception('读取曲目 info.json 失败')
                print(f'读取 {info} 失败:{e}')
                continue
            if not isinstance(n,dict):
                print(f'{info} 的内容不是 JSON 对象,跳过 {a}')
                continue

            try:
                name = n.get('name') or a
                fn = n.get('file','') or NO_FILE
                mv = n.get('mv','') or NO_FILE
                if not isinstance(name,str):
                    print(f'{info} 里的 name 不是字符串,改用目录名 {a}')
                    name = a
                if not isinstance(fn,str) or not isinstance(mv,str):
                    print(f'{info} 里的 file/mv 不是字符串,忽略这两个字段')
                    fn = fn if isinstance(fn,str) else ''
                    mv = mv if isinstance(mv,str) else ''
            except Exception as e:
                logging.exception('解析曲目 info.json 失败')
                print(f'解析 {info} 失败,跳过 {a}:{e}')
                continue
            if not name:
                print(f'跳过 {a}:名称为空')
                continue
            music = os.path.join(b,fn) if fn and os.path.isfile(os.path.join(b,fn)) else NO_FILE
            video = os.path.join(b,mv) if mv and os.path.isfile(os.path.join(b,mv)) else NO_FILE
            if music == NO_FILE and video == NO_FILE:
                print(f'跳过 {a}:没有可播放的文件')
                continue

            key = name
            i = 2
            while key in aaa:
                key = f'{name} ({i})'
                i += 1
            aaa[key] = {'music':music,'video':video}
        return aaa

    def run(self):
        self.app.mainloop()


def main():
    # 日志已在模块级按 __main__ 条件装好(见 _setup_logging),这里只兜一次底:
    # 万一是被别的方式拉起来的,也要保证日志落到 music.log。
    _setup_logging()

    # 审计钩子:插件绕开沙盒门面(内省拿到真 os/socket)时的第二道闸
    if start_plugin and start_sandbox:
        if install_audit_hook():
            print('[沙盒] 审计钩子已安装:绕开门面的文件/网络/进程访问同样会被拦')

    pro = Tkapp()
    pro.app.focus_get()
    pro.run()

    

pro:Tkapp
__env_id__:str

if __name__ == "__main__":
    try:
        

        main()
    except Exception:
        logging.exception('程序运行期间发生未捕获异常')

        traceback.print_exc()
        raise SystemExit(1)