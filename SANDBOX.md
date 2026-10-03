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

* **内省**:`SandboxDenied.__init__.__globals__` 能摸到本模块全局(含宿主凭据 `_HOST_TOKEN`);
  `pro.app.after.__closure__` 能顺着闭包摸到真实控件;
* **Tcl 通道**:插件画界面必须能 `import tkinter`,而 Tcl 自己就能起进程、读写文件,且是 C 层调用,
  **不产生 Python 审计事件**;
* **C 扩展**:直接调系统接口的扩展不在审计事件范围内。

所以沙盒的价值是:**默认拒绝 + 出事有日志 + 授权可追溯**,防的是"顺手越权"和事故,
不是"有备而来的恶意代码"。真隔离只能把插件放进单独进程,代价是插件不能再直接操作 Tk 界面。

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
* 插件如果自己在 `plugin/` 里放 `helper.py` 之类的模块,可以 `import`,但它们同样在沙盒里执行;
* 审计钩子装一次就撤不掉(进程级),这是 Python 的设计。

## 自测

```powershell
# 在项目根目录跑
python tests/run_all.py
```

`tests/` 下四个套件:核心能力(`test_sandbox_core.py`)、装载集成(`test_sandbox_integration.py`)、
运行期授权(`test_sandbox_grants.py`)、宿主门面与审计钩子(`test_sandbox_facade.py`,它会装进程级
审计钩子,所以排在最后)。也可以单独跑某一个。`plugin/nb/a.py` 是**越权样本夹具**(它在
`plugin.json` 里 `can_exec:false`,不会自动跑),测试里会被强制打开来验证它撞墙。
