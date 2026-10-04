# 插件沙盒

`plugin/` 下的插件现在跑在沙盒里。实现全在 [plugin_sandbox.py](plugin_sandbox.py),接入点在
[b.py](b.py) 的 `Plugin` / `Tkapp`。这份文档写给两类人:**装插件的人**(怎么授权、怎么撤销)和
**写插件的人**(能用什么、被拦了怎么办)。

## 三道闸

| 闸 | 实现 | 作用 |
|---|---|---|
| 能力策略 | `SandBox.can()` / `check_fs` / `check_net` / `check_proc` | 默认只允许插件**读自己的目录**;写文件、联网、起进程都要授权 |
| 宿主门面 | `HostFacade`(插件拿到的 `pro`) | 插件只能加菜单项、调白名单窗口方法、读播放列表快照;拿不到 `Tkapp`、`player`、`env_dict` |
| 审计钩子 | `install_audit_hook()`(`sys.addaudithook`) | 插件**绕开门面**(用内省拿到真的 `os`/`socket`/`subprocess`)去碰文件、网络、进程时,按同一套策略拦下 |

审计钩子只在"调用栈上确实有插件代码帧"时才动手,宿主(播放器自己)的访问不受影响。

## 不是安全边界(重要,请读完)

插件和播放器在**同一个进程、同一个解释器**里。下面这些能绕开上面三道闸 —— 这是方案的固有上限,不是待修的 bug:

* **内省**:`type(pro.menu).__init__.__globals__` 就是本模块的全局字典(含 `_HOST_TOKEN`);
  `__sandbox__.can.__self__` 直接是那个 `SandBox` 对象;异常回溯的 `f_globals` 也一样;
  `pro.app.after.__closure__` 能顺着闭包摸到真实控件;
* **Tcl 通道**:插件画界面必须能 `import tkinter`,而 Tcl 自己就能起进程、读写文件,且是 C 层调用,
  **不产生 Python 审计事件**;
* **C 扩展**:直接调系统接口的扩展不在审计事件范围内;
* **不在审计表里的事件**:`os.stat`/`os.lstat` 没有事件,所以"文件存不存在、多大"探测不到;
  读环境变量(`os.environ`)也不产生事件 —— 内省拿到真 `os` 之后,环境变量是读得到的。

所以沙盒的价值是:**默认拒绝 + 出事有日志 + 授权可追溯**,防的是"顺手越权"和事故,
不是"有备而来的恶意代码"。真隔离只能把插件放进单独进程,代价是插件不能再直接操作 Tk 界面。

### 已经堵掉的几条内省逃逸(2026-10 加固)

`plugin/escape` 是一个"越权样本/PoC"插件(默认 `can_exec:true`,要不要装载看你的
`allow_plugin`),它把上面那条内省链走了一遍。照着它做的加固:

| 逃逸手法 | 加固 |
|---|---|
| 偷 `_HOST_TOKEN` 后调 `box.set_unsafe(True,...)` 把自己放出来 | `set_unsafe`/`grant`/`env_box.create`/`env_dict.grant` 除凭据外**再加一道"调用栈上没有插件帧"**;插件从自己代码里调这些入口一律拒绝并记 `violation:set_unsafe` 之类 |
| 直接改 `box.policy.fs_read = ['C:\\']`、`policy.net/proc = True`(seal 只换了容器,没人拦赋值) | `Policy` 封存后(`freeze()`)拒绝一切改写;`SandBox.can()` 及审计钩子改读**封存时的只读快照** `_policy_frozen`,插件手上的 `policy` 只是展示用 |
| 往 `box._session` 里塞授权 | 封存时记下授权基线 `_org`(`policy ∪ 装载期已发的 session` 授权);出现基线外的条目会记一条 `violation:tamper`(判权仍按快照) |
| 撑大 `_ALWAYS_READABLE` 让钩子把"读任何文件"当白名单 | 钩子在装的时候就把解释器目录**固化成元组**存进闭包,并自己实现 `_hook_readable`,不再走模块级的 `_always_readable`(那条路每次调用都会重读 `_ALWAYS_READABLE`) |
| 清空 `_FRAME_TAGS`/`_FRAME_DIRS` 让钩子认不出插件帧 | 钩子闭包持有同一批表(换掉模块变量没用),另有 `_FrameRegistrar` 记账:表里的条目比记账少 => **按记账立刻把表恢复**,记一条 `violation:registry`(只报一次),然后**继续按调用者正常判定**——插件那一下照样被拒,宿主不受影响。注意千万别做成"拒绝一切":宿主自己的 `after`/`winfo`/`logging` 也走审计钩子,那样插件清一次表就能把播放器拖崩(实测崩在 `b.py` 的 `on_resize`) |

### 又一批:判权/审计"入口"本身也被锁了(2026-10)

上面那张表锁的是**策略数据**,但逃逸样本的最后几步证明判权**入口**还是普通实例属性:

| 逃逸手法 | 加固 |
|---|---|
| `box._policy_frozen = <unsafe 快照>`(判权快照是实例属性,谁都能换) | 判权数据整体搬进模块私有的记账表 `_AUTH`(`id(box) -> {policy, frozen, org, session, ...}`);封存后 `box._policy_frozen` / `box.policy` / `box._session` 只是**影子副本**,不参与判权。改它们只留一条 `violation:tamper` |
| `box.can = lambda cap, target=None: True`(门面和审计钩子都回调 `box.can`,换掉它 = **两道闸同时失明**) | 唯一判权实现在 `_auth_can()`(`SandBox.can()`、门面 `check_fs/check_net/check_proc`、审计钩子全走它);审计钩子在 `install_audit_hook()` 时就把 `_auth_can`/`_auth_entry` 收进闭包,**不再回调 `box.can`** |
| `box._session['fs:write'].add(r'C:\\')`(判权原来直接读这个 dict) | 封存时记账里存一份**独立的** session 副本,判权读它;`box._session` 变成影子。授权基线之外的条目只记 `violation:tamper` |
| `box.violation = lambda *a, **k: None`(拦得住但**记不下**) | `violation` / `note` / `events` 和 `can` 一起列进 `SandBox.guarded_names()`:封存后再赋值直接抛 `SandboxDenied` 并留痕 |
| 换掉 `box.can` 之后**门面自己**把真实 `open` 当"已授权"执行 —— `_guarded()` 又把审计钩子的 depth 置 1,钩子见 depth>0 就放行,**两道闸一起失效**(这条最阴:光把判权搬进记账还不够) | 门面里的判权不再调 `self.can`,一律走 `_auth_can()`;`_guarded()` 拿到 `_auth=(能力,目标)` 后会**先用记账复核**,"真授权才豁免审计钩子" |

`guarded_names()` 故意**不**锁 `_host`/`_facade`/`_ask`/`_persist`:那是宿主接口,封存之后宿主仍要合法地重挂(`b.py` 的 `Plugin.run()` 每次执行命令都会再 `attach_host` 一次);插件改了它们也拿不到额外能力。

`_guarded()` 的复核只对**记账里真有的授权**生效:用户在弹窗里点"只允许本次"属于不落账的一次性放行
(`check_fs` 的第二个返回值就是"是否记账授权"),那一次真实调用照样豁免审计钩子 ——
否则 `t_audit_allows_own_dir_and_facade` 那条"授权后仍被拦"的老坑会回来。

诚实说明剩下什么:同进程里插件终究能摸到那些对象,所以这仍然是"抬高门槛 + 会报警",
不是边界。`_AUDIT_HOOK` 内部出错时为了不把播放器带崩是**放行**的,所以"让钩子自己抛异常"
仍是一种绕法(会留下 `logging.exception` 记录)。

### 第三批:判权/审计依赖的**模块 globals** 本身可写(2026-10,`plugin/escape2`)

前两批把"策略数据"和"判权入口"都搬进了模块私有的 `_AUTH` 记账表 —— 但**记账表自己,
以及判权/审计用到的每一个函数,都还住在 `plugin_sandbox` 的模块 globals 里**。插件和它同进程,
一行内省就能拿到那个 dict,于是"模块私有"并不等于"插件改不了":

```python
g = type(__sandbox__).__init__.__globals__      # 就是 plugin_sandbox.__dict__
```

实测(`plugin/escape2`,自测 `dsh/escape2_selftest.py`)能真的走出去的路:

| 逃逸手法 | 为什么成立 | 拦住没 |
|---|---|---|
| `g['_FRAME_LOCAL'].depth = 5` | 审计钩子第一句就是 `if getattr(_depth,'depth',0): return`,而 `_depth` 抓的正是模块级 `_FRAME_LOCAL`。置位后**第二道闸对本线程全局失明**,真 `os` 全部放行 | ✗ 一行就够 |
| `g['_AUTH'][id(box)]['session']['fs:write'].add('C:\\')` | 记账表是普通 dict,`_auth_domain` 读的正是它;门面和钩子当场改判成"已授权" | ✗ |
| `g['_auth_entry'] = lambda box: <unsafe 记账>` | 钩子里 `ent = _auth_entry(box)`、门面里 `_auth_entry(self)` 都是**全局名查找**;换掉它 = 两道闸同时失明 | ✗ |
| `g['under'] = lambda r,p: True` | `_auth_can` 里 `any(under(r,target) ...)` 同样是全局名查找;只要 roots 非空就恒真(读权限尤其有效) | ✗ |
| 清空 `g['_FRAME_TAGS']`/`g['_FRAME_DIRS']` + `g['_frame_registrar'].count = 0` | 注册表的防篡改靠 `tampered()` 比对计数;把计数归零**再**清表,`_hook_frames_box` 恒返回 None,钩子认为"不是插件干的"直接放行 | ✗ |
| `compile(src,'<not-a-plugin>','exec')` + `g['threading']` 起一条新线程 | 钩子判断"这一帧是不是插件"靠 `co_filename`(`<plugin 名字 ` 前缀 / 插件目录),而 **filename 是插件自己传给 `compile` 的**;新线程的栈上只剩伪造帧,扫不到任何插件帧 → 判定"不是插件干的"放行 | ✗(第六批:`plugin_sandbox_fix_fake_filename.py` 把帧身份改成 **code 对象身份**,这条的"起线程"那一下也仍然要先拿到 proc 授权) |
| 同上,把回调交给白名单方法 `pro.app.after` | 真实 mainloop 里回调帧同样只有伪造文件名,而且 `after` **不产生审计事件**,连"拦 proc"这条退路都够不着 | ✗(真机;第六批给 `after`/`after_idle` 这条通道挂了**回调归属**,`plugin/test-1/a.py` + `dsh/fake_filename_selftest.py` 在真 Tk 主循环下实测由 OK 变 BLOCK) |

根因一句话:`install_audit_hook()` 只把**对象引用**收进了闭包。`_AUTH` 的**内容**、
`_auth_entry` / `under` / `_auth_frozen` 这些**函数本身**都不是闭包变量,替换或改写它们
不影响闭包,却影响所有按全局名查找的调用点。`_FrameRegistrar` 的计数校验同理 ——
计数和那两张表都是插件内省得到的对象上的普通属性。而最后一组更根本:**"插件帧"这个
身份本身是用 `co_filename` 字符串认的**,那是插件能自由指定的东西,所以它根本不是身份。

这不是"漏了一个补丁",而是方案定位的直接后果:**同进程内省拿得到 `__dict__`,所以模块里
没有真正的私有数据**。加固只能做成两件事:

1. 把内部调用点全部**固化**(默认参数 / 闭包),让"替换 globals 里的名字"不再影响判权;
2. 给记账加**完整性校验**,让"改写内容"变成会报警并当场拒绝的操作,而不是静默放行。

修复已经**并进 `plugin_sandbox.py` 本身**(2026-10 合并;过程中先在副本里验证过,
照 AGENTS.md 的约定当时没动原文件)。诚实说明:改完仍然不是边界 —— 置位
`_FRAME_LOCAL.depth` 这类手法只是从"一行静默失明"变成"会撞上校验并留 violation"。

### 第四批:宿主门面的内部数据直接挂在 `__slots__` 上(2026-10,`plugin/escape2`)

门面(`HostFacade` / `_AppFacade` / `_MenuFacade`)是插件唯一能看到宿主的地方,
白名单写得很紧 —— 但它把**真控件本身**放在 `__slots__` 的普通属性上:

    class _AppFacade:
        __slots__ = ('_sb','_app')      # ← 真 Tkapp 就躺在这儿

`__getattr__` **只对"找不到的属性"触发**。`_app` 是真实存在的 slot,
`pro.app._app` 根本走不到白名单那一句,于是:

| 逃逸手法 | 结果 |
|---|---|
| `pro.app._app` | 真 `Tk` 对象;`.tk.eval("exec ...")` 就是一条完整的 Tcl 起进程通道(实测 `expr 1+1` 返回 `2`) |
| `pro.menu._menu` | 真 `Menu` 控件,同一个 `.tk` 通道 |
| `pro._pro` | **整个原宿主对象**:`env_dict`(注册表)、`plugin_list`(别人的命名空间)、`music_player` 全回来了,门面等于没装 |

`_MenuFacade` 那句注释("不把绑定方法交出去,因为绑定方法的 `__self__` 是控件")
防的是**方法**,没防住**属性** —— 属性就是控件本身。`pro.app._app` 用自测夹具
(FakeApp)也能复现,`tk.eval` 那一步要真 Tk。

修复同样已并进 `plugin_sandbox.py`:真控件只存在一个"自己也会拦"的槽(`_d`)里,
插件读 `_app`/`_pro`/`_menu`/`_d` 照常撞白名单,并留一条 `violation:host_attr`。
复现脚本见下面自测一节的 `dsh/facade_probe.py`。

### 同批实测到两条"拦不住"的缺口(无解,不是漏补丁)

跟门面那两条一起跑出来的还有两条,性质不同 —— **CPython 根本没给这些操作发审计事件**,
第二道闸没有可挂的钩子:

| 手法 | 为什么拦不住 |
|---|---|
| 真 `os.environ` 读环境变量 | 读 `os.environ` 不产生任何审计事件;它是 `os` 模块上的普通对象,插件从内省拿到真 `os` 就能读。**第五批**把"不需要内省"的那几条路堵掉了(见下面第五批那一节),内省那条仍然通 |
| `os.stat` / `os.path.getsize` 探测目录外文件 | `_FS_READ_EVENTS` 只有 `os.listdir`/`os.scandir`;CPython 的审计表里**没有** `os.stat`/`os.lstat`(那条注释就写在 `_FS_READ_EVENTS` 旁边) |

实测:同一个插件帧里 `os.listdir('C:\Windows')` 被钩子拦下,而
`os.stat('C:\Windows\win.ini').st_size` 返回 `92`、`os.environ['PATH']` 直接读出来。

危害有上限:沙盒仍然拦得住**读内容**(`open`)和**列目录**,这两条只泄漏
"某个已知路径存不存在/多大"和进程环境变量(宿主可能在里面放了路径或凭据)。
真封死只有一条路:**把插件放进独立进程**,让它内省也摸不到宿主的地址空间。

另外,`plugin/escape2` 第 01/02 步(内省拿回 `SandBox`、从函数 `__globals__` 偷
`_HOST_TOKEN`)在加固前后都算"成功" —— 但那两步只是**拿到对象**,拿去用
(`set_unsafe`、换 `can`)都撞在第 03/05 步上。它们在清单里是"拿到",不是"越权"。

### 第五批:环境变量(`os.environ`)的可达路径(2026-10,bug 名 `environ`)

"读 `os.environ` 不产生审计事件"是真的,所以**审计钩子**这一道闸对它无解。但"钩子挂不上"
不等于"门面也拦不住" —— 实测(项目根目录跑,`plugin/escape2` 当插件目录)有三条**完全
不需要内省**的路,一行属性访问就能把 `PATH` 读出来:

| 手法 | 为什么成立 |
|---|---|
| `os._real.environ['PATH']`、`os._sb`、`os._wrap`、`os.path._real`(→`ntpath.os.environ`)、`sys._real.modules`、`io._real`、`Image._real`、`mutagen._real` | `_Proxy` 把真模块/沙盒/包装表放在**普通 `__slots__` 属性**上,而 `__getattr__` 只对"找不到的属性"触发 —— 和第四批门面(`pro.app._app`)是同一个 bug,只是当时只修了三个宿主门面,没修模块门面 |
| `os.path.expandvars('%PATH%')`、`os.path.expanduser('~')`、`Image.os.environ` | `expandvars`/`expanduser` 本来就是"读环境变量"的入口却在 allow 名单里;`Image` 是 `allow=None` 的门面,`PIL.Image` 的全局里挂着真 `os`,门面照原样交出去 |
| `logging.os.environ`、`random._os.environ`、`tkinter.sys.modules`、`tkinter.filedialog.os`、`ttkbootstrap.filedialog.os`、`ttkbootstrap.style._compat.os`、`ImageTk.Image.os` | `_PASSTHROUGH` 把 `json`/`logging`/`tkinter`/`ttkbootstrap`… **真模块**直接交给插件;真模块的全局里挂着 `os`/`sys`/子模块。从命名空间出发只顺着"模块属性"走一遍(BFS,深度 4)就能列出 **23 条**到达 `os.environ` 的链路 |

修法在副本 `plugin_sandbox_fix_environ.py`(原文件照 AGENTS.md 不动):

1. `_Proxy` 的内部数据(`_real`/`_sb`/`_label`/`_allow`/`_wrap`/`_deny`)从实例槽搬到
   **模块私有的弱引用表 `_PROXY_DATA`**,实例上**一个属性都不留**:`os._real`/`os._sb`/
   `os._wrap`/`os._d` 统统落到 `__getattr__`,被"下划线开头的名字一律不给"挡下并记
   `violation:attr`。**这里故意没沿用第四批的 `_d` 槽写法** —— 槽是类上的描述符,
   `type(o)._d.__get__(o)` / `object.__getattribute__(o,'_d')` 两头都能绕过
   `__getattribute__`(第四批那三个宿主门面现在还能这么读走真 Tkapp,见"已知的坑");
2. 门面**不再交出外来模块句柄**:取到的属性是模块对象时,只有"门面自己的子模块"
   (按模块名前缀判定:`tkinter.filedialog`、`ttkbootstrap.dialogs`、`PIL.ImageDraw`…)
   才递归包一层同样的门面交出去,其它模块(`os`/`sys`/`threading`/`fnmatch`/`logging`…)
   直接拒并记 `violation:module_attr`;
3. `os.path.expandvars`/`expanduser` 从 allow 名单挪进 deny;
4. **直通模块也套门面**:所有 `_PASSTHROUGH`(`json`/`logging`/`tkinter`/`ttkbootstrap`…)、
   `PIL.*` 子模块、`ImageTk` 全部走 `safe_module()`,规则同第 2 条;`urllib.parse` 也从
   "真模块"改成门面。`_resolve_module` 相应改成"先顺着门面属性把子模块门面拿到",保证
   `from tkinter.simpledialog import askstring` 这类写法仍然拿得到子模块(而 `import a.b`
   要的顶层包由 `import_module` 按 `__import__` 的契约返回)。

副本实测:`plugin/escape2` 当插件目录、在插件帧里跑,上面三类 **25 条读法全部 BLOCK**;
宿主侧顺着 **90 个 `allow=None` 门面**把每个属性都取一遍,**0 条**还能拿到真模块/`environ`;
24 条白名单用法(`os.path.join`/`os.listdir`/`open`/`logging`/`json`/`urllib.parse`/
`tkinter.ttk`/`filedialog`/`simpledialog`/`ttkbootstrap.dialogs`/`Image`/`ImageTk`/`mutagen`/
`io`/`sys.version`/`time`)与 10 种 import 写法全部照旧可用。`dsh/facade_probe.py`(16/17 全
BLOCK)与 `dsh/escape2_selftest.py`(ok=2 / blocked=11 / na=1)和原文件逐项一致,没有退化。

诚实说明仍然没堵住的:

* **内省**:`type(__sandbox__).__init__.__globals__['os'].environ`(escape2 第 13 步)、
  `os.path.join.__globals__['os']`、任何函数/类的 `__globals__`/`__closure__` 照样读得到环境
  变量 —— 根因和第三批一样(同进程里模块 globals 就是插件读得到的普通 dict),只有独立进程
  能封死;
* `os.stat`/`os.lstat` 没有审计事件那条**本批没动**(只修 `environ`)。

**合并要点**:直通模块套门面会改变 `import`/命名空间里"安全模块"的**身份**(`is`),功能不变。
`dsh/fix_regression.py` 拿副本跑四个套件,只有 `tests/test_sandbox_core.py` 的两个用例报错,
正好是这 6 行身份断言(原文件与测试文件按 AGENTS.md 都没动,合并时要一起改):

| 行 | 现在 | 改成 |
|---|---|---|
| 352 | `assert box.import_module('json') is json` | `assert box.import_module('json') is box.module_builders()['json']`(再加一句 `dumps` 的功能断言) |
| 386 | `assert box.import_module('tkinter.simpledialog') is tkinter` | `... is box.module_builders()['tkinter']` |
| 387 | `assert box.import_module('tkinter.simpledialog', fromlist=('askstring',)) is sd` | `assert (...).askstring is sd.askstring` |
| 399 | `assert ns['tkinter'] is tkinter` | `assert ns['tkinter'] is box.module_builders()['tkinter']` |
| 406 | `assert ns['_sub'] is sd` | `assert ns['_sub'].askstring is sd.askstring` |
| 407 | `assert ns['_ttk'] is tkinter.ttk` | `assert ns['_ttk'].Style is tkinter.ttk.Style` |

其余断言(353、371、388/389、400、410 的"没在白名单里的包照样拒",以及另外三个套件)
在新实现下全部通过。副本文件头的"合并说明"一节列了同样的 6 行,可以直接抄。

## 写插件:能声明什么

`plugin.json` 新增 `sandbox` 段(不写就是最严的默认值):

```json
{
  "name": "myplugin",
  "can_exec": true,
  "init_file": "a.py",
  "sandbox": {
    "fs_read":  [".", "../music"],   // 相对插件目录,也可以写绝对路径;默认 ["."]
    "fs_write": [],                  // 默认空:要用就在运行期向用户申请
    "net":  false,                   // socket / urllib / http
    "proc": false,                   // subprocess / os.system / ctypes
    "ask":  true,                    // 被拒时是否弹窗请求授权
    "unsafe": false,                 // true = 申请完全不受限制(装载时要用户确认)
    "modules": ["numpy"]             // 额外放行的顶层模块(不受门面代理,慎用)
  }
}
```

历史字段仍然兼容:`"privilege": ["no_sandbox_really"]` 等价于 `unsafe: true`;
`"privilege": ["built"]` 只打印提示(常用内置函数本来就可用)。未知键、类型写错都只会**更严**。

`unsafe` 与 `sandbox.unsafe` 都只是**申请**:只有你在装载确认框里点"是"、且名字进了
`config.json` 的 `plugin_unsafe` 之后才真的生效。

## 写插件:能用什么

插件命名空间里的东西(`from b import *` 那行会被自动去掉,也不需要写):

| 名字 | 说明 |
|---|---|
| `pro.menu` | `add_command` / `add_separator` / `add_checkbutton` / `add_radiobutton` |
| `pro.app` | 白名单:`after` `after_idle` `after_cancel` `title` `geometry` `deiconify` `withdraw` `iconify` `destroy` `update` `update_idletasks` `bind*` `unbind*` `resizable` `minsize` `maxsize` `winfo_width/height/x/y/exists/screen*` `attributes` |
| `pro.music_dict` | 播放列表的**深拷贝快照**(改了不影响播放器) |
| `pro.plugin_names` | 已加载插件的名字列表(不给对象) |
| `__sandbox__` | `can(能力,路径)` / `describe()` / `events()` |
| `SandboxDenied` | 沙盒拒绝时抛的异常,可以精确 `except` |
| `open` `os` `sys` `io` `json` `re` `math` `random` `time` `datetime` `collections` `itertools` `functools` `string` `textwrap` `traceback` `logging` `tkinter` `ttkbootstrap` `Image` `ImageTk` `mutagen` | 受控门面或安全模块;`os.environ` `os.system` `sys.modules` `sys.exit` `import ctypes/shutil/importlib/pickle/vlc/b` 等都是拒的 |
| `plugin_dir` `__env_id__` | 自己的目录、自己的 env 号 |

`pro` 之外拿不到 `player` / `env_dict` / `plugin_list` / `config`。想加播放控制或读别人的命名空间,
现在没有这条路 —— 需要的话提需求,加白名单方法比敞开后门好。

被拒绝时抛 `SandboxDenied`;如果 `ask` 打开、当前在 Tk 主线程、且这个目标没问过,沙盒会**先问用户**
(允许本次 / 本次运行都允许 / 总是允许 / 拒绝,默认按钮是"拒绝")。子线程里不弹窗,直接拒。

写插件时的建议:动手前先 `__sandbox__.can('fs:write', path)` 自查;把 `SandboxDenied` 当"操作没发生"处理,
不要整段 `try: ... except Exception: pass` 吞掉。

## 装插件:你会看到什么

* 装载白名单外的插件时,确认框会列出它申请的权限;
* 插件声明了 `unsafe` 时,会单独问一次"它将以播放器的全部权限运行…是否允许?";
* 插件运行期越界时弹窗,选"总是允许"会写进 `config.json`,以后自动生效。

`config.json` 里新增两个字段:

```json
{
  "plugin_unsafe": ["debug"],
  "plugin_grants": {
    "debug": {"fs_write": ["D:\\k\\music\\.venv"], "net": true}
  }
}
```

撤销授权:直接删掉对应条目(或整个字段)再启动即可。**白名单 `allow_plugin` 只管"要不要加载";
`plugin_grants` 才是"允许它碰什么"。**

## 已知的坑与行为约定

* 白名单是**目录级**的:对某个文件点"总是允许",实际放开的是它所在的目录,`config.json` 里记的也是目录;
* 共用同一个 `env_id` 的两个插件是同一个信任域,策略会**合并**(装载时打印合并结果),权限取并集;
* 只读探测(`os.path.exists` 这类)不弹窗,没权限就安静地返回 `False`/`0`;
* **`os.stat`/`os.lstat` 不产生 Python 审计事件**(CPython 的审计表里就没有),
  所以"内省 + stat"能探测到插件目录外文件是否存在、有多大 —— 挡不住,只能算已知缺口;
* `pro.music_dict` 是快照:读得到、改不动;
* **封存(`seal()`)之后 `can`/`violation`/`note`/`events`/`policy`/`_session`/
  `_policy_frozen`/`_org` 这些名字在 `SandBox` 上不可再赋值**(直接抛 `SandboxDenied`)。
  这是有意的:它们是判权与审计入口。宿主如果在封存后还需要改策略,那是设计外的用法;
  `_host`/`_facade`/`_ask`/`_persist` 不在这个名单里,因为 `b.py` 每次执行插件命令都会
  重新 `attach_host`;
* 判权数据放在模块私有的 `_AUTH` 里,**但"模块私有"挡不住同进程内省**:
  `type(__sandbox__).__init__.__globals__` 拿到的就是这个模块的字典,插件能读到
  `_AUTH` 本身并改写它的**内容**(见上面"第三批"第 07 步)。
  现在的 `plugin_sandbox.py` 用"授权基线过滤 + 调用点固化"把这条路抬高了,
  但抬高不是封死;
* **第四批那三个宿主门面的 `_d` 槽能被"类描述符"绕开(2026-10 实测,未修;bug 名 `facade_d_descriptor`)**:
  `pro.app._d` 确实被 `__getattribute__` 拦着,但 `_d` 本身是**类上的
  `member_descriptor`** —— `type(pro.app)._d.__get__(pro.app)`(或者
  `object.__getattribute__(pro.app,'_d')`,一个内置名就够,不需要内省)照样把
  `(sandbox, 真 Tkapp)` 交出来:实测
  `type(pro.app)._d.__get__(pro.app)[1].tk.eval('expr 1+1')` 返回 `'2'`,
  `type(pro)._d.__get__(pro)[1].env_dict` 直接读到宿主注册表。也就是说第四批堵掉了
  `pro.app._app` 这条**属性**路,但**描述符**那条没堵。
  第五批的模块门面(`_Proxy`)故意没沿用 `_d` 槽,就是为了避开这一类(数据放模块私有的
  弱引用表 `_PROXY_DATA` 里,实例上不留任何属性);宿主门面这一处按 AGENTS.md 只登记、
  不在 `plugin_sandbox_fix_environ.py` 里动 —— 想修就照第五批改动 1 把 `_d` 也搬进表里;
* 插件如果自己在 `plugin/` 里放 `helper.py` 之类的模块,可以 `import`,但它们同样在沙盒里执行;
* 审计钩子装一次就撤不掉(进程级),这是 Python 的设计;
* **`co_filename` 不是身份(2026-10 实测并已在副本里收紧;bug 名 `fake_filename`)**:
  第六批之前,钩子判断"这一帧是不是插件"靠文件名前缀 / 插件目录前缀,而那是插件
  自己传给 `compile()` 的字符串。`plugin/test-1/a.py` 把它武器化成了真机版本:
  伪造 `co_filename` + `pro.app.after`(真 mainloop 是**异步**的,回调跑起来时插件
  帧早已返回),`proc:false` 的插件于是把 `os.system` 跑通
  (`dsh/fake_filename_selftest.py` 用真 `tkinter.Tk()` 复现;`plugin/escape2` 的
  第 12/13 步是同一根因,自测夹具同步所以只能记 N/A)。
  `plugin_sandbox_fix_fake_filename.py` 把判据换成 **code 对象身份**
  (`register_plugin_code` 登记宿主编译的那棵 code 树)+ 给 `after`/`after_idle`
  挂**回调归属**,实测这一路被拦。仍然没堵的:`bind`/`bind_all` 这类"把回调交给
  别的执行上下文"的通道没挂归属(本批只做了 `after` 两条);`pro.app._d` 描述符
  那条老路(上一段)照旧;
* `tests/test_sandbox_integration.py` 里写死了"会被装载的插件清单"和沙盒数量,
  所以 `plugin/` 下**新增一个会装载的插件**(现在的 `plugin/escape` 和
  `plugin/escape2`),那个套件的 `t_wired` 要跟着一起改;`test_sandbox_core.py`
  的 `t_escape_fixture` 按名字点 `plugin/nb`,别把那个夹具删了。

## 自测

```powershell
# 在项目根目录跑
python tests/run_all.py
```

`tests/` 下四个套件:核心能力(`test_sandbox_core.py`)、装载集成(`test_sandbox_integration.py`)、
运行期授权(`test_sandbox_grants.py`)、宿主门面与审计钩子(`test_sandbox_facade.py`,它会装进程级
审计钩子,所以排在最后)。也可以单独跑某一个。`plugin/nb/a.py` 是**越权样本夹具**(它在
`plugin.json` 里 `can_exec:false`,不会自动跑),测试里会被强制打开来验证它撞墙。

加固用的临时验证脚本(`.gitignore` 里的 `dsh*`)放在 `dsh/`:

```powershell
# 前两批(策略数据/判权入口)的逃逸样本,修复后应当"越权成功的项: 无"
.\.venv\Scripts\python.exe .\dsh\escape_selftest.py
# 只验审计钩子闭包与注册表记账
.\.venv\Scripts\python.exe .\dsh\hook_probe.py
# 专验两个曾经踩过的坑:清表之后宿主还能不能干活、撑大白名单还顶不顶用
.\.venv\Scripts\python.exe .\dsh\fix_check.py
```

第三/四批的样本是 `plugin/escape2`,配套三个脚本:

```powershell
# 越权样本:加固前实测 9 条"越权成功"(06~12 真的走出去了),
# 加固后应当只剩 2 条 —— 都是"拿到对象/偷到凭据"这类前置动作,不是越权
.\.venv\Scripts\python.exe .\dsh\escape2_selftest.py
# 门面穿透专项(第 16/17 步):pro.app._app / pro._pro / pro.menu._menu / Tcl eval,
# 应当全部 BLOCK,而白名单方法(pro.app.after 等)仍然可用
.\.venv\Scripts\python.exe .\dsh\facade_probe.py
# 完整的四个套件;默认跑当前的 plugin_sandbox.py
.\.venv\Scripts\python.exe .\dsh\fix_regression.py
```

`escape2_selftest.py` 认 `ESCAPE2_FIX` 环境变量(把 `sys.modules['plugin_sandbox']`
换成指定模块再跑同一套样本),想对比"加固前/后"时用它,平时不用设。
`facade_probe.py` 认 `FACADE_MODULE`,同理。

第五批(`environ`)的副本是 `plugin_sandbox_fix_environ.py`,用同样两个环境变量就能拿它跑同一批样本:

```powershell
$env:ESCAPE2_FIX = 'plugin_sandbox_fix_environ'
.\.venv\Scripts\python.exe .\dsh\fix_regression.py     # 只有 test_sandbox_core 的那 6 行身份断言要改
Remove-Item Env:\ESCAPE2_FIX
$env:FACADE_MODULE = 'plugin_sandbox_fix_environ'
.\.venv\Scripts\python.exe .\dsh\facade_probe.py       # 16/17 仍全 BLOCK
Remove-Item Env:\FACADE_MODULE
```

第六批(`fake_filename`,`co_filename` 不是身份)的副本是 `plugin_sandbox_fix_fake_filename.py`:

```powershell
# 复现/验证主路:真 tkinter.Tk() 主循环下,伪造 co_filename + pro.app.after
#   - 原 plugin_sandbox.py      => exit 2,"拦不住"(marker 文件被 os.system 造出来)
#   - plugin_sandbox_fix_...    => exit 0,"拦住了伪造帧,而且宿主未被误伤"
.\.venv\Scripts\python.exe .\dsh\fake_filename_selftest.py
$env:ESCAPE2_FIX = 'plugin_sandbox_fix_fake_filename'
.\.venv\Scripts\python.exe .\dsh\fake_filename_selftest.py
# 顺手探边界:门面 after / 真 Tcl 通道(.tk)都被拦;bind 那条本批没挂归属
.\.venv\Scripts\python.exe .\dsh\fake_filename_probe.py
# 四个套件:facade / integration 那两套的夹具换成 dsh/ 里的副本(原文件不动)——
# facade 原来手工 compile 插件帧,新判据下那种帧不算插件代码,夹具要像真实装载
# 那样登记;integration 只是把新加的 plugin/test-1 补进它自己写明"要跟着加"的清单。
# 结果:grants / facade / integration 全绿,core 只剩它那两条既有失败
# (t_import / t_import_dotted,和本批无关,基线一样红)
.\.venv\Scripts\python.exe .\dsh\fix_regression.py --facade-fixture
Remove-Item Env:\ESCAPE2_FIX
```

**宿主侧零改动**:先前那版修复是"`b.py` 装载时多调一次 `self._box.register_plugin_code(code)`",
但那要改 `b.py`(项目原有文件,按 AGENTS.md 不能直接动),所以改成**加固自己在
`SandBox.__init__` 里登记**:它本来就收得到 `plugin_dir`,于是自己读
`plugin.json`、按 `b.py` 的三处细节(`init_file`/`command_file` 覆盖字符串字段、
`.replace('from b import *','',1)`)取出源码,用同一个文件名
`<plugin {name} init>` 编译出**和宿主将要执行的那一棵等价**的 code 对象并登记。
`b.py` 一行没改,逃逸样本照样被拦(实测)。宿主若自己 `compile` 插件代码,还可以用
`SandBox.exec_plugin_code(code, ns)` 交出去执行,由它负责登记。

注意:`plugin/escape2/plugin.json` 现在是 `"can_exec": false`,而 `escape2_selftest.py`
按 `b.Plugin(..., set())` 装载、不会强制打开它,于是脚本会死在
`next(v for k, v in cmds.items() if '越权报告' in k)` 的 `StopIteration` 上 —— 这是**当前仓库状态**
的问题,不是沙盒的问题(拿 `plugin_sandbox.py` 跑也一样)。想看样本结论,把那个 `can_exec` 改成
`true`(或自测脚本里 `n.can_exec = True` 之后再 `init_i`);上面"ok=2 / blocked=11 / na=1"
就是这么测出来的,原文件与副本逐项一致。

这些脚本都只调用插件的报告回调/直接调函数,**不点任何菜单、不弹窗**,所以不会卡住。
另外 `dsh/` 下别用管道(`|`):这个工作区里管道会被沙盒判成 `file access denied`。
