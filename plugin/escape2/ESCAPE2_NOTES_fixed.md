# escape2 文档声明更正（D1 / D3）

本文件是审查产物，用于更正同目录 `a.py` 顶部 docstring 中**与实测不符**的声明。
`a.py` 原文件未作任何改动（工作区约定：不得直接更改原有文件）。

## 1. 被更正的说法

`a.py` docstring 第 15-26 行用表格宣称下列 5 条路径**成立**：

| 原声明 | 实测结果 | 结论 |
|---|---|---|
| `g['_FRAME_LOCAL'].depth = 5` → 审计钩子第一句 `if getattr(_depth,'depth',0): return`，置位后第二道闸全局失明 | **BLOCK** | 声明错误 |
| 往 `g['_AUTH'][id(box)]['session']` 塞 `C:\` → 判权恒真 | **BLOCK**（`can=False`） | 声明错误 |
| `g['_auth_entry'] = <unsafe 记账>` → 钩子和门面同时失明 | **BLOCK** | 声明错误 |
| `g['under'] = lambda r,p: True` → 判权恒真 | **BLOCK** | 声明错误 |
| 清 `_FRAME_TAGS`/`_FRAME_DIRS` + `g['_frame_registrar'].count = 0` → 钩子认不出插件帧 | **BLOCK**（已按记账重建 1 条） | 声明错误 |
| `compile(src,'<not-a-plugin>','exec')` + 新线程 → 钩子放行 | **BLOCK** | 声明已过时 |

实测汇总（`dsh/escape2_selftest.py` 与修订版 `dsh/escape2_selftest_fixed.py` 均可复现）：

```
汇总:OK=2  BLOCK=11  N/A=1
  越权成功: 01 内省拿回 SandBox 与模块 globals
  越权成功: 02 偷 _HOST_TOKEN(函数 __globals__)
```

这 2 项**只是侦察**：拿到对象与凭据不等于取得越权能力。样本自己的第 03 步
（用偷来的凭据调 `set_unsafe`）与第 04 步（调 `grant`）均为 **BLOCK**，
第 02 步的 detail 也写着「拿到也未必能用」。

## 2. 为什么原声明会过期

`a.py` 的 docstring 写于**第三批**加固时期。此后落地了后续批次，把原声明依赖的
机制全部换掉了：

- **判权数据搬进私有 `_AUTH` 记账**，且 `_auth_can` / `_auth_entry` / `_auth_domain`
  通过**默认参数在函数定义时绑定**（`def _auth_can(ent,cap,target=None,_under=under,...)`）。
  因此事后替换模块级 `under` / `_auth_entry` 这两个全局名**不再有任何效果** —— 闭包与
  默认参数早已持有原函数对象。
- **`_AUTH` 的“内容”确实仍可被插件改写**（`_AUTH` 是模块级 dict，`_auth_entry(box,_auth=_AUTH)`
  绑定的就是同一个对象），但 `_auth_domain` 读的是**冻结快照 `ent['frozen']`**，且
  `_auth_tamper` 会检测 `session` 与基线 `org` 的差异并把改动记为 `violation:tamper`。
  实测 `can('fs:write','C:\\')` 仍为 `False`。
- **帧身份从 `co_filename` 字符串改为 code 对象身份**，并引入 `_frame_registrar` /
  `_CodeLedger` 记账；注册表被清空时钩子会**按记账重建**，所以“清零计数”不再有效。
- `g['_FRAME_LOCAL'].depth` **从来不是**声明里描述的那道闸。全文检索确认：`depth`
  只有写入（`_facade_enter` / `_facade_exit`）与一处捕获，**没有任何地方读它做判断**；
  真正的重入保护是 `_hook_in_facade_frame()` 用 code 身份实现的。

## 3. 修订版沙盒里的变化

- `_FRAME_LOCAL.depth` 及其写入函数 `_facade_enter` / `_facade_exit` 已作为**死状态删除**
  （保留 `_FRAME_LOCAL`，因为 `.warned` 仍在用）。
- 帧身份改为 **fail-closed**：当 `co_filename` 命中已注册的插件 tag / 插件目录前缀，
  但 code 对象不在记账里时，**按它自称的那个插件判权**（原先 `return None` 等于按宿主放行）。
  这直接堵掉了「动态 `compile()` 自定义 `co_filename` 逃避审计钩子」的路子。

## 4. 更正后的自测结论口径

原 `dsh/escape2_selftest.py:148` 用 `escaped = list(res['ok'])` 判定，把上述 2 项**侦察**
也算作逃逸，于是打印「=> 沙盒被拆(模块 globals 这条路成立,bug 复现)」——**与它自己的
汇总输出矛盾**。修订版 `dsh/escape2_selftest_fixed.py` 把侦察步骤单列，只有真正取得
越权能力（解除限制 / 读出目录外文件 / 起进程）才算逃逸。
