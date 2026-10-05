# 开发说明

面向本项目开发者。用户使用说明见 [README](../README.md)。

## 项目结构

```
campus_login.py         主程序（单文件，无参=GUI；命令行开关见 `SWITCH_HELP`，或用 --help 打印）
palette.py              界面配色的**唯一来源**（程序与站点 CSS 共用一份，见「界面配色」一节）
paths.py                统一的路径解析（不要在脚本里写死绝对路径）
proc_tree.py            进程树管理：Job Object + 看门狗 + _MEI 残留清理
build.py                构建入口，带五道门槛（含域名一致性，见下）
run_tests.py            测试统一入口：fast / env / gate 三档 + `--audit` 查漏登记
domain_drift_test.py    站点域名「多处不漂移」一致性测试（构建门槛，源码级）
school_profile_test.py  学校参数：换学校只改 config.json 真的生效（含阳性对照）
savepoint.py            本地存档：改代码前打快照，搞砸了能回退
ast_bugscan.py          AST 级 bug 扫描（自查用，带阳性/阴性对照）
stack_probe.py          卡住时打印所有线程堆栈（排障用）
local_secrets.example.py  本地凭据模板（复制成 local_secrets.py 填真值，已被 gitignore）
docs/                   README 用的界面截图与本文件
```

`palette.py` 和 `updater.py` 一样是**顶层 import** —— PyInstaller 的 `--onefile` 是「纯脚本目录」，
不走包机制，所以同级模块顶层 import 就会被打包进去（藏在函数里的 import 反而容易被漏掉）。

## 测试与诊断脚本

**先用统一入口跑一遍**，别靠脑子记「这次该跑哪几个」：

```bash
python run_tests.py            # = fast，纯逻辑，秒级（13 个）
python run_tests.py env        # 要 Tk / 真桌面 / pylnk3（12 个）
python run_tests.py gate       # 需要一个已构建的 exe（5 个）
python run_tests.py gate <exe> # 指定 exe；不传就用 paths.find_exe()
python run_tests.py --list     # 全部清单（含**永不自动跑**的 manual 档，附原因）
python run_tests.py --audit    # 只查「有没有 *_test.py / *_probe.py 漏登记」
```

三档的分界就是「在无人值守会话里跑不跑得动」。三个设计要点：

- **`--audit` 是防漏接的**。新写了 `*_test.py` 却忘了登记进 `run_tests.py`，它就红。
  项目里那三十多个「本来该跑、只是没人接」的脚本就是这么攒出来的 ——
  跑不动的**也要登记**，放 `MANUAL` 并写清原因。
- **输出走临时文件，不走管道**。理由同 `build.py` 里那条坑：`--onefile` 的
  bootloader 会起子进程，孙进程继承写端；孙进程不退，管道就永远等不到 EOF，
  **设了 timeout 也照样挂死**。写文件没有「等 EOF」这回事。
- 超时不是 `p.kill()` 就完事 —— 这些脚本会起 Tk / 子进程 / 甚至 exe，
  必须 `proc_tree.kill_tree(p.pid)` 连整棵树一起收。

⚠️ 本脚本**不建 Tk 窗口**，所以它在哪种解释器下跑都行；但它**跑的那些脚本**可能
需要 tkinter / pylnk3，所以跑 `env` / `gate` 档前先确认解释器齐（见下）。

### 分档分错会长什么样（2026-10-05 实测）

第一版 `env` 档塞了 18 个，实跑 **12 通过 / 6 没通过**。逐个查下来，
**6 个「失败」里 5 个是分档错，只有 1 个是真 bug**：

| 脚本 | 表面 | 实际 |
|---|---|---|
| `logout_fail_probe.py` | 失败：`start_job 用 state["busy"] 判重入` | 🔴 **真回归** —— 它按源码文本断言，`run_gui` 拆成 `LoginApp` 后 `state` 变成了 `self.state`，字面量对不上了 |
| `updater_test.py` | 超时 240s | 受限会话把回环连接丢弃（脚本自己的 docstring 就写了），换个环境就好 |
| `watchdog_gil_test.py` | 失败 | **概率性**复现 Tk 卡死，复现不到就报「什么都没验到」，脚本自己写了「常复现不到」 |
| `fresh_test.py` | 超时 + 1 项 FAIL | 断言「数据目录落在 `ProgramData` 下」，而受限会话里 `ProgramData` 不可写、程序**正确地**退回 `%APPDATA%`；末尾 Tk 收尾还会卡 |
| `tk_probe.py` | 超时 241s | 诊断探针，打印完事件表后 Tk 收尾卡住 |
| `tk_exit_race_test.py` | 失败（退出码 1） | 它验的是「`os._exit` 会卡」这个**已知结论**，结论成立时**故意退 1** —— 1 是「判断成立」，不是「失败」 |

**两条教训**：

1. **「按源码文本断言」的测试是重构的隐形地雷。** 那个真回归之所以能被发现，
   纯粹是因为统一入口终于把 `logout_fail_probe.py` 跑起来了 —— 它平时没人跑。
   现在它改用**容忍 `self.` 前缀的正则**，下次再重构不用回来改。
2. **`MANUAL` 不只是「会动真实账号」。** 还应该收：
   ① 断言依赖本机条件（`ProgramData` 可写 / 真回环网络）的；
   ② 概率性、跑不到就是「什么都没验到」的；
   ③ 结论成立时故意退非 0 的诊断探针。
   这三类留在 `env` 档里，`run_tests.py env` 就会**永远红**，而「永远红」等于没人看。
   代价是这些脚本平时跑不到 —— 所以 `--list` 会把它们连原因一起打出来。

| 脚本 | 作用 | 能不能当门槛 |
|---|---|---|
| `run_tests.py` | **统一入口**：`fast` / `env` / `gate` 三档 + `--list` / `--audit`。不建 Tk，哪种解释器都能跑 | 是（已可当入口，未接进 build.py） |
| `leak_check.py` | 隐私检查：exe 里有没有夹带账号密码 | 是门槛 |
| `portability_check.py` | 可移植性：换台电脑能不能跑 | 是门槛 |
| `domain_drift_test.py` | 站点域名「7 个文件说的是同一个域名」；**源码级**，故意放在打包之前 | 是门槛 |
| `school_profile_test.py` | 学校参数（`triggerUrl` / `checkTargets` / `loginExtra` / `campusSubnets`…）改 config.json 真的生效；含阳性对照与「写坏了要退回默认」 | 否 |
| `secret_scan.py` | **提交前**扫凭据 / 本机绝对路径 | 提交前跑（`--staged`） |
| `help_check.py` | 内置使用说明确实进了 exe | 否（可以加进 build.py） |
| `wiring_gate_test.py` | 验「接线自检」这道门槛的报警链路本身通不通 | 否 |
| `ui_contrast_test.py` | 验「按钮底色不能和背景同色」这条判定会拦人（假控件树，**不建 Tk**） | 否 |
| `ui_contrast_gate_test.py` | 端到端验同一条门槛：注入「隐形按钮」→ `--guitest` 必须 GUI_FAIL（要真桌面） | 否 |
| `datasafe.py` | 数据沙箱：把数据目录换到临时目录 + 拦截越界写入 + 事后核对 md5。**任何跑 `run_gui()` 的脚本都必须先用它** | 否（跑自检 `python datasafe.py`） |
| `content_h_probe.py` | 量出**本机真实状态**下的内容高度，并按结构拆到每一张卡片（改界面竖向间距后重新标定 `geometry_test.CONTENT_H`） | 否（诊断用） |
| `content_h_matrix.py` | 把内容高度在**各种运行状态**下量一遍（账号数 / 自启警告 / 账号行有无「上次成功」），给出真实范围 | 否（诊断用） |
| `verify_exe.py` | 只验不打包；可传 exe 路径，用来拿旧版本做阳性对照 | 否 |
| `pwd_test.py` | 密码框显示规则（纯函数） | 否 |
| `poll_test.py` | 界面轮询队列：出错要不要记日志、会不会中断整批 | 否 |
| `pwd_gui_test.py` | 密码框真实窗口绑定 | 否（要真桌面） |
| `geometry_test.py` | 9 种屏幕尺寸下窗口不超出屏幕 | 否 |
| `proc_tree_test.py` | 进程树与看门狗，含「故意复现孤儿进程」 | 否 |
| `autostart_test.py` | 开机自启的增删改查（**计划任务 + `.lnk` 两条路**）、`.lnk` 参数读法、`autostart_stale` / `autostart_outdated`、**升级迁移不被构建产物污染** | 否 |
| `guard_test.py` | 守护判断表、守护标记自愈、用户主动退出的避让、`do_auto` 重试、`.lnk` 参数读法 | 否 |
| `network_state_test.py` | 校园网判定 / MAC 校验 / `network_state` 四态 / 门户 MAC 替换。⚠️ 第 2 节是**真机实测**，本机 IP 不在校园网段时**明确跳过**而不是假红（2026-10-05 改） | 否 |
| `logout_fail_probe.py` | 两个窗口并发点「退出」→ 复现「退出失败」。段 1 按源码断言（**容忍 `self.` 前缀的正则**，别写死字面量）；段 2 用假门户。自带 `datasafe` 沙箱 | 否 |
| `auto_timing_probe.py` | 把每次 `--auto` 的耗时按阶段拆开（定位「自启慢」慢在哪一段） | 否（诊断用） |
| `ui_shot.py` / `layout_probe.py` | 截图 / 量布局（要真桌面） | 否 |
| `png_zoom.py` | 裁切放大 PNG 局部、数字形个数（核对截图里画了什么） | 否 |
| `savepoint.py` | 本地存档：改代码前打快照，搞砸了能整体回退 | 否（改代码前用） |
| `ast_bugscan.py` | AST 级 bug 扫描：重复字典键 / 重复定义 / `is` 比字面量 / 可变默认参数 / 空 except / `finally` 里 return / `if x==1 or 2` / 赋值后未用 | 否（自查用，带阳性+阴性对照） |
| `stack_probe.py` | 跑到卡住时把**所有线程**的堆栈打出来（`faulthandler`） | 否（排障用） |

跑测试要用**带 PyInstaller 的解释器**（`leak_check` / `help_check` / `portability_check` 依赖它）。
`autostart_test.py` / `guard_test.py` 要写 `.lnk`，需要 **pylnk3**。
实测（2026-10-04）只有 `envs/build` 这一个环境是齐的：

| 环境 | 有什么 | 拿来干嘛 |
|---|---|---|
| `envs/build/Scripts/python.exe` | pylnk3 + PyInstaller + **tkinter** | 跑测试、跑 Tk 脚本、打包 —— **默认就用它** |
| `envs/default/Scripts/python.exe` | 只有 pylnk3 | 基本用不上 |
| 托管的 `versions/3.13.12` | **没有 tkinter、没有 pylnk3** | 只适合跑不碰 Tk / 不碰 `.lnk` 的纯逻辑脚本 |

> 本文档上一版写的是「pylnk3 只有 `envs/gui314` 里有」——**那个环境根本不存在**。
> 照它去跑会得到一个 `ModuleNotFoundError`，然后误以为「依赖没装」。
> 2026-10-04 实测修正。

缺依赖时 `guard_test.py` 会明确报错退出（码 3），**不会静默跳过**那几条断言。
`autostart_test.py` 会把 `task_state` / `task_create` / `task_delete` 换成桩（**不碰真实的计划任务**），
但它**真的会读写启动文件夹里的 `.lnk`** —— 跑完记得核一眼自启状态。

构建门槛的细节：`build.py` 的第 2 步（冒烟测试）里，自检失败会输出 `GUI_FAIL` 标记。
`--guitest` 本身是**软门槛**（`must=False`，建 Tk 窗口在无人值守会话里不稳定），
所以「界面没建起来」只打印一行参考；**只有命中硬拦标识才判失败**。硬拦标识是一组，
从 `campus_login` 导入、不许在 `build.py` 里另写一份字面量：

| 标识 | 谁抛的 | 管什么 |
|---|---|---|
| `WIRING_FAIL_PHRASE`（`密码框接线自检失败`） | `pwd_wiring_check()` | `form()` 接错成显示文本 / 忘了打码 |
| `UI_FAIL_PHRASE`（`界面自检失败`） | `ui_contrast_check()` | 按钮底色和背景同色（按钮隐形） |

⚠️ 加新的界面自检时，**必须**把自己的标识串加进 `build.py` 的 `HARD_FAIL_PHRASES`。
2026-10-04 实测踩到：配色自检一开始没有自己的串，它抛的 `GUI_FAIL` 被当成「Tk 环境抖动」
打印一行就放过去了 —— 门槛看着接上了，实际一次都没拦过。
`wiring_gate_test.py` 第 4b 节就是防这个：逐个核对每个标识都被 `build.py` 认得
（直接 `import build` 问它，不 grep 源码）。

## 🔴 测试脚本不许写真实数据（`datasafe.py`，2026-10-04 事故）

**发生过什么**：写了一个「只量界面高度」的探针 `content_h_matrix.py`，它把
`load_accounts` 打桩成合成账号（6 个假号 + 1 个真号）。跑完之后真实
`C:\ProgramData\CampusLogin\accounts.json` 里**真的多出 6 个假账号**，
真实账号的 `lastSuccess` / `lastAttempt` / `lastResult` 被清空。

**根因**：`run_gui()` 启动时走

```
load_all() → mutate_accounts(_do) → save_accounts(store)
```

而 `mutate_accounts()` 内部调的是**模块全局的** `load_accounts()` ——
也就是**被打桩的那个**。于是打桩只改变了「读什么」，完全没阻止「写回去」。
这类脚本最要命的地方是：**意图（只读）和行为（写盘）之间没有任何提示**，
跑完打印一堆高度数字，看起来一切正常。

**现在有三道闸**（`datasafe.py`，缺一不可）：

| 闸 | 做什么 | 拦住什么 |
|---|---|---|
| 重定向 | `_DATA_DIR` + 4 个路径常量一起指到临时目录 | 让写落不到真身上（⚠️ 光改 `_DATA_DIR` 不够，`CONFIG_FILE` 是**导入期**算好的） |
| 写拦截 | 包一层 `write_json`，目标不在沙箱里就**当场抛异常** | 唯一能「当场」拦住写操作的闸 |
| 事后核对 | 记下真实数据文件的 md5，收尾比对 | 前两道都失效时至少能**知道**出事了 |

用法：

```python
import datasafe
datasafe.sandbox(copy_real=True)   # 必须在 run_gui() 之前
C.run_gui()
print(datasafe.assert_untouched()) # 收尾核对
```

`copy_real=True` 会先把真实数据**复制**进临时目录 —— 量到的仍然是「这台机器真实
状态」的界面，但真身一个字节都不动。**跑 `run_gui()` 的脚本一律这么用。**

### 🔴 探针要量「真的有数据那个目录」（`datasafe.real_dir()`）

2026-10-04 圆角化那轮踩到：`content_h_probe.py` 量出来是 **814**，而真实状态是
**830** —— 静默偏了 16px，而且**偏差方向恰好让人以为是「改动变矮了」**，差一点
就照着这个错数字去改 `CONTENT_H`。

根因链（每一环单看都是「对」的）：

1. 受限环境里**删文件**被安全策略拦下（重定向到回收站后 `os.remove` 抛 `OSError`）；
2. `data_dir()` 的写测试 `_writable()` 要求「写得进**且删得掉**」—— 于是它判 False；
3. `data_dir()` **正确地**退回 `%APPDATA%\CampusLogin`；
4. 那个目录里**没有** `accounts.json` → 界面渲染成「暂无保存的账号」→ 814。

`datasafe.real_dir()` 现在**不再盲信 `C.data_dir()`**，而是扫描全部候选目录
（`ProgramData` → `Roaming`），**哪个里面真的有 `accounts.json` 就用哪个**；
一个都没有才退回第一个候选。`snapshot()` / `assert_untouched()` 也从「盯一个目录」
改成「盯**全部候选目录**」（本机是 12 个文件）—— 只核对一个目录时，写到了另一个
目录是**查不出来**的。

⚠️ **已知环境残留**：`C:\ProgramData\CampusLogin\.write_probe`（1 字节）是
`_writable()` 删不掉留下的。这是环境限制，不是缺陷，`portability_check.py` 里
已经文档化。

### ⚠️ 探针的读数可能是「假读数」

同一轮里，`content_h_probe.py` 报「账号行数 1」—— 而当时列表里其实是**空的**，
那一个子控件是「暂无保存的账号」这句 Label，被公式 `(n+1)//2` 数成了 1 行。
**这个假读数正好和当时的预期吻合**，于是被当成「数据没问题」的证据。

教训：**探针的每一行输出都要问一句「它凭什么这么算」**。数子控件反推行数是
最容易出假读数的地方，先认「空列表的那句话」再套公式。同理，`layout_probe.py`
里「一个复选框都没找到」现在会记进 `PROBLEMS`，而不是安静地跳过那一节。

**当时怎么把数据救回来的**：没有备份，但有两条证据 ——

1. `campus-login.log` 里最后一次 `认证成功` 是 `2026-09-30 11:51:31`；
   最后一次真的跑到 `portal_login` 是 `2026-10-01 11:42:15`
   （`update_account_record` 只在 `portal_login` 之后调用，靠 `[耗时] 登录` 那行定位）。
2. 内存里记着原始文件的 **md5 基线**。

拿这两条去**穷举 144 种字段组合**，只有一个的 md5 命中基线 —— 于是**字节级还原**，
不是「大概填一个」。还原后又跑了一遍 `content_h_probe.py`，得到的 `reqh=830`、
账号卡 `127px` 与事故前那次测量**完全一致**，算第二条独立佐证。

**给以后的教训**：

- 「只读的脚本」是个危险的说法。判断依据是**它调用了什么**，不是**它想干什么**。
- 加沙箱的同时必须写**负向对照**（往真实路径写必须抛异常）—— 只测「沙箱内能写」
  等于什么都没测，因为沙箱没生效时正向也是通过的。
- 探针脚本的 `run_gui()` / `exit_hard()` **不能写在模块级**：那样 `import` 它
  就等于跑一遍并硬退进程。我拿它当库复用时，后面写的打桩代码一行都没执行，
  输出看着还正常（只是数字不对），**静默骗了我一次**。见 `content_h_probe.py`
  末尾的 `if __name__` 守卫。

### 从源码跑 GUI 检查要用 `pythonw.exe`

实测（2026-09-17）：用 `python.exe`（控制台子系统）跑 `--guitest`，程序会**卡住不退出**，
连 `os._exit(0)` 都杀不掉进程。最小复现是 `tk.Tk(); withdraw(); update_idletasks(); destroy()` ——
只要 Tk 窗口真正实例化过，带控制台的进程在 `ExitProcess` 的 DLL detach 阶段就会挂住。
换成 `pythonw.exe`（GUI 子系统，无控制台）立刻正常退出。

打包出来的 exe 是 `--windowed`，属于 GUI 子系统，不受影响（`--guitest` 3.3s 退出码 0）。
所以：从源码验界面用 `pythonw.exe`，或者直接验 exe（`verify_exe.py`）。
`build.py` 的门槛跑的是 exe，因此一直是准的。

## 界面截图是怎么生成的

README 里那几张截图由 `python ui_shot.py` 生成，输出到 `ui_shots/`。
它开跑前会先 `sandbox()` —— 把数据目录换到 `%TEMP%\campus_ui_shot` 并写入假账号
（`2026000001` 之类），所以窗口里根本没有真实学号可拍。

这一步不能省，也**不能靠事后检查**：截图里的文字是光栅化像素，把 PNG 解压出来
搜学号是搜不到的（连窗口标题都搜不到），那种检查只会给出假阴性。只能从数据源头隔离。

想**亲眼核对** `docs/` 里那几张图（唯一可靠的验证方式），用 `png_zoom.py`
把可疑区域放大来看：

```bash
python png_zoom.py docs/password-masked.png --cols 498,524,50,400   # 数字形个数
python png_zoom.py docs/password-masked.png --crop 52,494,68,34 --scale 10
```

实测这两条命令能确认密码框里是「8 个星号 + 明文的 `4`」，也就是沙箱里的假密码
`demo-1234` 露出末位 —— 而不是真实密码。

## 本地开发：别把自己的密码提交上去

```bash
cp local_secrets.example.py local_secrets.py   # 填你自己的学号 / 密码
python secret_scan.py --staged                 # 每次提交前跑一遍
```

`local_secrets.py` 已在 `.gitignore` 里，**不会**被提交、也**不会**被打进 exe。
它只供本机测试脚本用（真实登录测试、隐私检查的搜索词）。

**密码一旦提交就删不掉了。** `git rm` 只删除后续版本，历史里仍然留着 ——
要清干净得改写历史 + 强制推送，而且别人已经 clone 的副本、GitHub 的缓存
都收不回来。所以要在**提交之前**拦，这就是 `secret_scan.py` 存在的理由。

## 改代码之前先打个存档

```bash
python savepoint.py save "改密码框之前"        # 动手前先存一份
python savepoint.py list                       # 看有哪些存档
python savepoint.py restore 2026-09-17_1329    # 搞砸了就退回去
python savepoint.py diff                       # 先看看现在跟存档差在哪
```

存档走的是**另一套引用** `refs/savepoints/*`，不是普通提交 —— 所以：

- 不污染 `git log`，不动 HEAD，不改工作区（打快照对仓库是只读的）
- **不会被 `git push --tags` 带出去**（它不是 tag），仓库要公开时这点很重要
- 一直被引用着，所以永远不会被 gc 回收
- 快照内容是那一刻工作区的完整状态（含未提交修改、新增、删除），但**遵守 .gitignore**
- ⚠️ 但 `git push --mirror` **会**把它带出去（`--all` / `--tags` 都不会，只有 `--mirror` 会）。
  而存档里可能存着「改代码之前」的旧版本 —— **包括当时还没清理掉的真实学号 / 密码**
  （本仓库实测确实有这种 blob）。所以：永远不要对这个仓库用 `--mirror`，
  也不要把 `refs/savepoints/*` 显式推上去。要确认某个存档里有没有凭据，
  用 `secret_scan.py` 扫它的 blob，别靠猜。

`restore` 之前会**自动再存一份**，所以「回退」这个动作本身也能再回退，脚本会把
反悔命令直接打在屏幕上。

> ⚠️ 脚本里**绝不调用 `git prune` / `git gc`**。本机实测：在这个工作区里跑
> `git prune`（任何形式，连默认两周宽限期的普通版也会）会把整个对象库清空，
> `git status` 报 `fatal: bad object HEAD`，连初始提交都没了。
> 清快照用 `tidy`（只删引用，不碰对象）。

## 开机自启：为什么用计划任务，而不是启动文件夹

2026-09-20 用本机事件日志逐帧量出来的同一次开机：

| 时刻 | 事件 | 来源 |
|---|---|---|
| 09:32:33.5 | OS 启动 | `Kernel-General` id=12 |
| 09:32:52.7 | 用户登录 | `Winlogon` id=1 |
| 09:32:57.3 | `GHelper.exe` | 计划任务（登录时触发） |
| 09:32:59.3 | `PowerToys.exe` | 计划任务（登录时触发） |
| 09:33:13.3 | 桌面就绪 | `Shell-Core` id=9648 |
| 09:33:38.0 | `HKCU\...\Run` 第一项才开始跑 | `Shell-Core` id=9705 |
| 09:33:58.2 | **我们**（启动文件夹的 `.lnk`） | 同上 |

登录触发的计划任务在开机后 **25 秒** 就跑起来了，启动文件夹要等到 **85 秒**，差 **约 60 秒**。
而程序自己跑完认证只要 **0.3 秒**（onefile 解压另算约 2 秒）——
**慢的从来不是认证逻辑，是「什么时候轮到我们」。**

所以自启改成：优先在任务计划程序里建一个叫 `CampusLogin` 的**登录触发**任务，
建不了（策略限制等）才退回启动文件夹的 `.lnk`。两条路**互斥** ——
建了任务就删掉 `.lnk`，否则会同时触发两份。

几个实现要点：

- 任务定义文件 `C:\Windows\System32\Tasks\CampusLogin` **普通用户能按名字直接 `open()` 读**
  （UTF-16LE，含明文 command / arguments），但 `os.listdir` 那个目录会被拒绝访问 ——
  所以判断自启状态**不用拉 PowerShell**（省约 1 秒）。见 `task_state()`。
- 建任务走 `Register-ScheduledTask -Xml`，XML 必须是 **UTF-16LE + BOM**。
  `MultipleInstancesPolicy=IgnoreNew`、`DisallowStartIfOnBatteries=false`、
  `ExecutionTimeLimit=PT0S` 都要显式写 —— 默认值分别是「再起一份」「拔电就不跑」「72 小时」。
- 注册成功**不代表任务长对了** → `task_create()` 建完立刻回读核对 command / arguments / trigger。
- 老用户升级迁移在 `autostart_upgrade()`：第一次跑 0.3.2 时把 `.lnk` 换成任务，
  用 `C:\ProgramData\CampusLogin\autostart-upgrade.json` 记「做过了」。

> 🔴 **别让构建的冒烟测试污染用户的自启。** `build.py` 会拿 `distN/CampusLogin.exe` 跑一次
> `--selftest`，那次运行如果也去迁移，就会把自启指到**构建产物**上、还顺手删掉用户的 `.lnk`，
> 一次构建就把用户的机器搞坏（2026-09-20 真踩到）。所以 `autostart_upgrade()` 有两道守卫：
> ① `.lnk` 存在但不是全部指向当前 exe → 跳过；② 任务存在、指向别处、又没有 `.lnk` 可参照 → 跳过。
> **跳过时绝不能写升级标记** —— 标记是全机共享的，写了真正装的那份下次就被跳过，
> 修复对用户等于没发生。回归用例见 `autostart_test.py [12]`。

## 自我更新：临时文件放哪，为什么

`updater.work_dir_for(exe, work_dir)` 决定更新临时文件（`.new` / `.new.part` / `.old`）
落在哪个目录。**必须是程序数据目录，不是 exe 同目录。**

2026-09-20 用户反馈：「检查更新并下载后，会在 exe 那个文件夹残留 `.old`；
如果程序放在桌面，这个操作就会污染桌面，让小白不知所措。」
根因就是临时文件按 `os.path.dirname(exe)` 拼路径。

四个要点：

- **必须判同卷**（`_same_volume`）。`os.replace` / `os.rename` 在 Windows 上走
  MoveFileEx、**不带 `COPY_ALLOWED`**，跨卷直接 `ERROR_NOT_SAME_DEVICE`。
  数据目录在别的盘时只能退回 exe 同目录 —— 宁可脏一点，也不能更新不了。
- **`.old` 是「下次启动才删」的**：替换时旧 exe 正被当前进程占用，`os.remove` 会被拒
  （而 `os.rename` 反而能成功，这是 Windows 上「运行中替换自己」的机制）。
  所以启动时的 `cleanup_stale()` 必须**两个位置都扫**，否则 0.3.2 及之前留在
  用户桌面的历史残留永远清不掉 —— 只清新位置等于没修好。
- **`cleanup_stale` 按前缀扫，不写死文件名**：`apply_update` 在 `.old` 被占着时
  （「更新完没重启，又点了一次更新」）会退到带时间戳的备用名 `<exe名>.old.<ts>`。
  写死名字就漏掉它，那种残骸会在数据目录里越积越多。
- **界面路径替换失败时要主动清 `.new`**：那是 12 MB 的东西，留着纯属浪费。

## 更新残留：为什么「清一次」不够（0.3.4）

上面那条修完之后，用户仍然报「更新后 `.exe.old` 残留、桌面没出现新版本」。
实测（工作区根目录 `_repro_real_path.py` / `_repro_lock.py`）挖出**两个**真问题：

**① 启动清扫会删掉正在下载的文件 —— 直接让更新失败。**

`cleanup_stale` 原本无条件删 `<exe名>.new` / `.new.part`。而**一次更新进行中**
只要有第二个实例启动（守护进程、用户又双击了图标、开机自启任务），它的启动清扫
就会把第一个实例刚下好的 12 MB 安装包删掉；紧接着 `apply_update` 的 `os.replace`
报 `WinError 2 系统找不到指定的文件`，更新整个失败并回滚。
用户看到的就是「升级失败 / 桌面没有出现新版本」。
复现日志里能直接看到那句 WinError 2。

修法：`.new` / `.part` 加**年龄门槛** `STALE_MIN_AGE`（10 分钟）——
比它新的不动（那几乎必然是正在下载）。`.old` **不设门槛**：它只可能是替换完成后的
废料，而且替换进行中它被占用、本来也删不掉。

**② 清不掉就永远不清 —— 所以「更新完 `.old` 还在」。**

替换完成后，旧 exe 改名成的 `.old` 往往**还被人占着**：最典型的是守护进程，
更新发生时它正跑着那个 exe，改名后它继续从 `.old` 跑完剩下的时间（最长 30 分钟），
这期间 Windows 拒绝删除。而启动清扫原本只跑一次、失败就算了 —— `.old` 就一直在。
`restart_self()` 又是「先起新的、再退旧的」，第一个清理请求几乎必然撞上占用。

修法：`sweep_stale_once()` 清一次，还有剩的就交给 `sweep_stale_retry()`
按 `STALE_SWEEP_DELAYS` 在后台重试，窗口盖过守护的 30 分钟。
`cleanup_stale_all()` 返回 `(删掉的, 没删掉的)` —— 重试需要知道「还剩什么」，
老签名 `cleanup_stale()` 只报第一个删掉的名字，信息不够。

**可观测性**：更新全程现在都写日志（下载落点、下载结果、替换结果、`.old` 最终位置），
`--selftest` 也多了一行「更新残留」。以前这条路径**一行日志都没有**，
用户报「更新没生效」时只能靠猜 —— 这次排查绕了很久，就是吃了这个亏。

**回归门槛**：
- `updater.py` 自检新增 4 项，其中最关键的是「★ 刚下好的 `.new` 不许被清掉」，
  以及反向的「把 mtime 改老之后必须被收掉」（否则「永不删」也能让前者通过 = 空转）。
- `updtest_full_chain.py` 第 7a3 步端到端验同一件事（种在 exe 同目录，
  不碰用户真实数据目录）。
- `verify_exe.py` 新增 `check_stale_leftover()`，并**自带阳性对照**：
  0.3.3 及更早的 exe 里没有「更新残留」这一行，拿它跑必然失败。

**验证通道**：`campus_login.py --selftest` 会报一行「更新临时目录」，
`verify_exe.py` 断言它等于数据目录（跨卷回退到 exe 同目录时按预期放行）。
GUI 里那段路径计算是个闭包、脚本点不到 —— **这一行是这条修复唯一能被自动化断言的
地方**，`update_dest_path()` 就是为此从闭包里抽出来的。
真进程回归用例见 `updater_test.py` 第 4b 节（含「连点两次更新」和
「exe 同目录自始至终只有 exe 一个文件」）。

## 清除凭据：为什么「删干净」比删更难（0.3.5）

**为什么要加这个入口**：清凭据原本只有命令行 `--purge`。对普通用户等于没有 ——
他要的是「别让这台电脑再留着我的密码」，不该逼他先学会开终端。
所以设置页最下面加了一个小链接（和「检查更新」同款，**不新增第四个主按钮**）。

**① 🔴 清完重启，账号会自己回来（连密码一起）。**

这是加界面入口时才暴露的真 bug，0.3.4 就存在，只是没人用命令行所以没撞上。

`migrate_legacy_data()` 的判据是「新位置没有 `config.json` 就继承旧位置」——
而**清凭据恰好制造了这个条件**。于是清完重启，账号密码从 `%APPDATA%\CampusLogin`
原样回来。单看「删除」那一步永远是成功的，**只有「删完再重启」才暴露**。

修法两条一起上，缺一不可：
- `purge_local_data(legacy=True)` **连旧位置一起清**（旧位置的文件在结果里用完整路径
  标出，否则界面上两个 `config.json` 分不清）。
- 落一个 `PURGED_FLAG`（`data_dir()/purged.flag`），`migrate` 看到就 `return False`。
  ⚠️ **这个标记绝不能进 `PRIVATE_FILES`** —— 否则 purge 每次把它自己删掉，标记等于没留。

复现与验证脚本：工作区根目录 `probes/_purge_reinherit.py`（先复现「清了又回来」，
修完再跑；含阳性对照「把标记删掉 → 照旧继承」，证明继承逻辑没被写坏）。

**② 🔴 顺序不能反：先 `note_logout()`，再 `purge_local_data()`。**

`note_logout()` 写的是 `accounts.json`，而 purge 会删它。反过来的话，
purge 刚删完又被重建出来，等于没清干净。这条顺序全项目只有一处需要保证，
所以抽成了 `purge_local_data_with_marker()`。

**③ 🔴 守护是另一个进程，删文件删不掉它内存里那份。**

`run_guard` 在**循环外**就把账号密码读进了局部变量，整个 30 分钟不重读。
用户点的是「清除」，程序里却还有一份密码，这不能算清干净。
修法：循环体最前面加 `guard_should_exit_for_purge()`，判据**只认「文件不存在」**
（不用 `load_config()` 的结果 —— 它读失败时同样返回空密码，磁盘抖一下就会误退 =
掉线再没人重连）。

**④ 日志不许撒谎。** 退出原因做成变量 `why_exit`（默认「已跑满 N 秒」）。
不做的话，因凭据被清而提前退出时日志会写「已跑满」，排查的人被带偏。

**回归门槛**：
- `updtest_purge.py`（工作区根目录，不在仓库）—— 20 条断言。两条必须成对：
  **正向**「清了 → 守护提前退出 <8 秒」+ **反向**「不清 → 至少跑满 2.8 秒」。
  没有反向，「提前退出」也可能只是循环压根没跑起来。
  另含「清完没有 `accounts.json`」（顺序没反）、「旧位置那份也清了」、「标记写下了」、
  「`purged.flag` 不在 `PRIVATE_FILES` 里」。
- ⚠️ `guard_test.py` **根本不测 `run_guard`**（它测的是 `do_auto` 的重试）——
  `--selftest` 走不到守护循环，所以**守护那段只有 `updtest_purge.py` 在盯**。
  改 `run_guard` 必须跑它。

## 更新来源：让用户知道程序在跟谁说话（0.3.5）

`updater.source_hint()` 一句话统一口径（`--checkupdate` 落盘 + GUI 确认框 + 结果提示
都用它）。域名**不硬编码**，唯一来源是 `MANIFEST_URL`；`HELP_TEXT` 里那份写死的域名
由 `help_check.py` 拿 `BASE_URL_HINT` 反算比对 —— 和「守护 30 分钟」同一个套路
（文案里必须写字面量才搜得到，再用反算值防漂移）。

用户是自己点「检查更新」才看到这行的，不存在打扰；而他有权知道这程序在跟哪个地址说话。
这也是排障时「检查更新失败」要问的第一个问题。

## 窗口高度：两个「注释自洽、实际是错的」常量（0.3.6）

有人反馈设置窗口在 16:10 的笔记本屏上「上下太长了」。诊断过程值得记下来，
因为两个病根里有一个是**注释在骗人**。

### 判 DPI 缩放不能看 `LOGPIXELSX`

| 量 | 这台机器上的值 |
|---|---|
| 物理分辨率 | 2560x1600 |
| `GetDeviceCaps(dc, LOGPIXELSX)` | 96 → **看着像 100% 缩放** |
| Tk 看到的（未声明感知时） | 1707x1067 |

`2560 / 1707 = 1.4997` —— 实际是 **150% 缩放**。系统 DPI 和显示缩放是两个概念，
在这台机器上并不一致。**判缩放要用「物理分辨率 ÷ 未声明感知时的虚拟分辨率」。**

顺带解释 `ui_shot.py` 里那条「不要声明 DPI 感知」：声明之后 Tk 看到的是物理像素，
窗口几何跟着变大（实测 820x811 → 1148x1135），截出来就不是用户看到的样子了。

### 病根一：高度上限是死数

原来 `h = max(560, min(1000, reqh + EXTRA_H))`，**完全没看屏幕多高**。
在这块屏上算出 963 / 1067 = 占屏高 90%，上下几乎没有余量。

改成 `SCREEN_H_RATIO = 0.85` 与原有的 `sh - SCREEN_MARGIN_H` **取更严的那个** ——
大屏靠比例收口，小屏靠 `sh-80` 收口，「绝不超出屏幕」这条原有保证一点没松。

### 病根二：`EXTRA_H = 40` 是纯富余

它的注释写「给标题栏/边框留的余量」。**这是错的**：Tk 的 `geometry("WxH")`
里的 H **就是客户区高度、不含标题栏**。实测：

```
root.winfo_geometry()    = 820x851
root.winfo_height()      = 851      ← 客户区就是 851，标题栏没拿走任何东西
holder.winfo_reqheight() = 783
holder.winfo_height()    = 783      ← 需要多少就给多少，富余 0
851 - 783                = 68       ← 28 外层留白 + 40 EXTRA_H，全是白的
```

也就是说这 40px 从来不是被标题栏吃掉的，而是一路变成卡片底部的空白。已归零。

**通用教训：注释解释了某个东西的存在，不等于它真的在起作用。** 一个常量可以带着
「自洽但错误」的注释活很久 —— 在窗口不吃紧的年代，它的副作用（底部多 40px 空白）
没人会注意。**要量，不要读注释。**

### 效果与门槛

本机（1707x1067）：holder 887 → 783、窗口 963 → **811**、占屏 90.3% → **76.0%**、
底部富余 68 → 0。小屏仍按需挂滚动条。

`geometry_test.py` 新增两节覆盖这次改动：第 7 节断言 16:10 屏上窗口不占满屏高、
上下留白 ≥ 20px；第 8 节断言 `EXTRA_H` 归零后窗口高正好等于传入的 `reqh`。

⚠️ 改产品行为后记得回头搜「有没有测试还在断言旧行为」—— 本次 `geometry_test` 的
`CONTENT_H/EXPECTED_H` 和 `ui_shot.py` 注释里的实例数字都需要同步。
另外第 4 节那条「720 高 → h=640」的断言不能只顾着让它变绿：比例上限生效后正解是
612，已改成断言 `min(sh*0.85, sh-80)` 并加一条「比旧的更矮」的反向确认。

⚠️ 几何常量**只有 `campus_login.py` 和 `geometry_test.py` 碰**。
⚠️ 极端小屏（400x300 以下）算出的 `h > sh` 是**旧版就有的既有语义**（`max_h` 有 320
的下限 floor），不是这次引入的，别当 bug 去「修」。

## 界面配色、圆角与图标

配色的**唯一来源**是 `palette.py` 里那组 `BG` / `CARD` / `FG` / `BLUE` /
`OK` / `DANGER` / `VIOLET` / `CYAN` … 常量（`campus_login.py` 顶层 import 它们，
`--help` 的说明窗口也用同一套）。改配色只改那一处，别在控件旁边写字面量。
每种强调色都配了一个 `*_SOFT` 浅底（`BLUE_SOFT` / `OK_SOFT` / `DANGER_SOFT` /
`VIOLET_SOFT` / `CYAN_SOFT`）—— 图标徽章和小标签用的都是「深色前景 + `*_SOFT` 浅底」这一对。

> **2026-10-05 之前不是这样**：这 19 个常量长在 `campus_login.py` 里，而站点那边
> （`make_site.py` 的 `CSS`）另有一份**手工同步的副本** —— 实测两边重合 15 个，
> 改一次配色要记得改两处，迟早会漏。现在站点 CSS 的 `:root` 常量块由
> `make_site.py` 的 `palette_css()` **从 `palette.py` 生成**，`= palette.X` 的注释
> 就是标给读的人看的：哪些值是生成的、哪些是站点独有的（`--bg` / `--code` /
> 整个深色模式块…）。
>
> ⚠️ 站点里**还留着一批站点独有的色**（深色模式、代码块底色…），它们**不**跟着程序走。
> 不要为了「统一」把深色模式也并进 `palette.py` —— 程序界面没有深色模式。

### 五个圆角工厂（Tk 一个圆角控件都没有）

| 工厂 | 画什么 |
|---|---|
| `round_button()` | 按钮（胶囊） |
| `round_panel()` | 卡片 / 输入框 / 说明窗口正文区（圆角容器） |
| `round_chip()` | 小标签、可点的小链接（可带 `command` 当按钮用） |
| `round_check()` | 复选框 |
| `icon_badge()` | 分区标题左边那个彩色圆角图标 |

底下的图元都出自 `_draw_round_rect(cv, x1, y1, x2, y2, r, fill, tags=None)` ——
**两个矩形 + 四个 `create_arc(style="pieslice")`**。

- 🔴 **不要用 `create_polygon(smooth=True)`**：Tk 的 smooth 是过线段中点的二次 B 样条，
  顶点只当控制点 —— 形状会整体缩进、实际圆角半径只剩一半。
- 🔴 **`2r >= 短边` 时它静默退回画直角矩形**（不报错、不留日志）。所以胶囊按钮的半径
  必须用 `pill_radius(h) = max(4, h // 2 - 1)`，**不能写 `h // 2`** —— 那正好踩在边界上，
  圆角会无声消失，而按钮看着「只是没那么圆」，很难发现。
- `tags=` 是给**会重画的东西**留的（卡片底），重画时只 `delete` 自己那个 tag。

### 圆角容器的五个坑（`round_panel`）

1. 🔴 **`create_window()` 放进去的 body 是画布上的一个图元** —— 所以重画时
   **绝不能 `delete("all")`**，那会把整个内容一起删掉（界面变空，而且不报错）。
   只删 `ROUND_PANEL_TAG`。
2. 🔴 **`pad_x >= radius` 且 `pad_y >= radius` 是硬约束**：body 是矩形，伸进角上的
   圆弧区就会用白方块把圆角盖掉，**且不抛异常**。`round_panel` 里直接 assert 住。
3. **Canvas 高度不会跟内容走**：必须把 `body.winfo_reqheight() + 2*pad_y` 回填，
   并**防自激** —— 记一个 `memo["h"]`。`configure(height=…)` 会触发 `<Configure>`
   再调回来，不记住上次的值就是死循环。
4. **`stretch=True` 时高度交给 `fill`/`expand`**，自己不能再 `configure(height)`，
   否则跟布局管理器互相打架。说明窗口的正文区用这个模式。
5. **滚动条必须放在圆角卡外面**（兄弟节点，坐在页面底色上）：Tk 的 `Scrollbar`
   是系统原生控件、只有直角，塞进圆角卡里那条直边会一直顶在圆角上。

### 圆角按钮 `round_button` 的兼容契约

- **它是工厂函数不是类**：本文件顶层**不 import tkinter**（所有 Tk 用法都延迟导入），
  这样 `--version` / `--purge` / `--selftest` 不必加载 Tk，`build.py` 才能安全 import。
  写成 `class X(tk.Canvas)` 就把 tkinter 拉进导入期了。
- **兼容接口只实现用得到的**：`configure(state=…)`、`invoke()`、`cget("state")`。
  短别名 `config` 故意不做 —— 它和 `configure` 是两个不同的函数对象，漏了别名
  `btn.config(state=…)` 会绕过拦截直接抛 TclError（响亮地报错，好过静默失效）。
- 🔴 **静止态底色不能和它坐的背景同色**，否则按钮整个隐形，且**不报任何错**。
  2026-10-04 实测踩到：「使用说明」的底色写成了 `#eef1f6`，而那正是页面底色。
  现在由 `ui_contrast_check()` 在 `--guitest` 里核对（不相等 + 色差 ≥ 8），
  失败抛 `UI_FAIL_PHRASE` → `build.py` 硬拦。**加了新按钮就顺手跑一遍 `--guitest`。**

### 自绘控件的两个静默坑

- 🔴 **Canvas 上的 `create_text` 内容查不出来**：`winfo_children()` 看不到文字。
  自绘控件必须把文字挂成属性（按钮 `_rb_text`、复选框 `_ck_text`），否则
  `ui_shot.py` 按文字找按钮、`layout_probe.py` 数复选框时会**静默找不到** ——
  后面的流程照样跑完，看着像成功。两个探针已经一并认这两个属性了，
  **新加自绘控件就顺手把探针的识别也补上**。
- **Tk 的 `bind` 是覆盖不是叠加**：整行 hover 和按钮自身 hover 不能共存，
  想要两层效果得自己在回调里分发。

### 图标徽章（`icon_badge` / `_ICON_SHAPES`）

分区标题左边那个彩色圆角小方块，里面是线框图标，共七个：
`list` / `user` / `power` / `lock` / `shield` / `globe` / `down`。

- 🔴 **图标名写错时抛 `ValueError`，不静默画一个空徽章** —— 空徽章看起来只是
  「图标没显示」，很容易被当成渲染问题查半天。
- ⚠️ **13px 下细节会糊成一个点**：「列表」原本是三个 2.6/24 的圆点，实际只有 1.4
  像素，渲染出来就是一条竖线。改成三条横线（长度递减，免得像汉堡菜单）。
  这么小的尺寸上**只画轮廓、别画细节**。
- 徽章是「深色前景 + `*_SOFT` 浅底」成对出现，配色表是 `SEC_STYLE`；
  分区标题**没登记就直接报错**，免得漏配时它默默退回无徽章的旧样式。

### ✅ 「使用说明」窗口的两份实现已经合一（2026-10-05）

以前这里是**两份**实现，改一处必须改另一处：

- `show_text_window()` —— 命令行 `--help` 用（模块级函数）
- `run_gui.show_help()` —— 界面里点「使用说明」用（闭包）

2026-10-04 圆角化时**只改了前者**，跑 `ui_shot.py` 一看第 5 张弹窗图的
**字节数和改动前一模一样（55862）**，才反过来发现是两份实现。

现在正文 + 按钮栏收敛到两个模块级函数，两边都调它们：

| 函数 | 管什么 |
|---|---|
| `text_panel(parent, tk, fam, base, text)` | 可滚动、可复制的只读长文本区，返回 `(外框, Text)` |
| `text_panel_bar(parent, tk, fam, base, command)` | 底部那条「知道了」按钮栏 |

- ⚠️ **`--help` 那条路可能没有 root 窗口**，所以 `text_panel` 只依赖传进来的 `parent`，
  自己不去找 Tk 根窗口 —— 这是它能被两处共用的前提。
- ✅ 合一之后验证过：`ui_shot.py` 第 5 张图在「合一前 vs 合一后」**逐像素 0 差异**。

### ⚠️ 比截图别跨工具比（2026-10-05 踩到的坑）

判断「界面有没有被改动」，**必须在同一个抓图工具下前后比**：

- `ui_shot.py` 与 `refresh_screenshot.py` 抓同一份界面，结果**不是**逐像素相同的 ——
  实测有约 2.5 万个像素差在 9..24 差量（文字亚像素抗锯齿 + 鼠标悬停的那个按钮）。
  拿 `site_static/screenshot.png`（`refresh_screenshot.py` 出的）去比 `ui_shot.py`
  的产物，会得到一片「有差异」的假警报。
- 还有个**必然的 1 行差异**：y=31 那一行左边 66px。它是**操作系统画的窗口边框行**
  （颜色 `(159,161,164)` **不在 `palette.py` 里**，且 Tk 从不画那条线），
  客户区从它下面才开始。跨工具、跨版本都可能差这一行，**别去追**。
- 判断方法：把差异按**颜色差量分档**（`<=8` / `<=24` / `<=64` / `>64`）。
  **只有 `>64` 才算结构性改动**；一片 `<=24` 就是抗锯齿噪声。
  `pixdiff2.py` 就是这个思路（放在工作区，不属于仓库）。
- ⚠️ 比之前先确认工具**自身可重复**：同一份代码连跑两次，md5 必须一样。
  不一样就说明它在抓实时状态（鼠标位置、网络状态行），md5 对比无意义。

### `run_gui` 已经拆成 `LoginApp` 类（2026-10-05）

`run_gui()` 原来是**一个约 1000 行的函数**，里面套了 25 个嵌套闭包，状态全是闭包变量。
现在：

- `class LoginApp` —— 状态是 `self.*` 属性，一眼列全；每个动作是一个方法，能单独读。
- `def run_gui(smoke=False)` —— **保留这个名字**，只是转发 `return LoginApp(smoke).run()`。
  `main()` / `ui_shot.py` / `layout_probe.py` / `content_h_probe.py` / `content_h_matrix.py` /
  `fresh_test.py` 全都按这个名字调它，所以**调用方一行都没改**。

拆分时**只搬家、没顺手改任何数值或顺序**（控件创建顺序、`pack` 参数、每处 pady 原样搬）。
验收方式：

| 检查 | 结果 |
|---|---|
| `ui_shot.py` 拆类前 vs 后 | **0 个结构性差异**（只有抗锯齿级噪声） |
| `content_h_probe.py` 的 `reqh` | **827**，与拆类前、与 `geometry_test.CONTENT_H` 一致 |
| 窗口几何 | `820x827+443+80`，与拆类前一致 |
| `--guitest` / `pwd_wiring_check` / `ui_contrast_check` | 全过 |

- ⚠️ `tk` / `messagebox` 存成了 `self.tk` / `self.messagebox` —— 仍是**在 `__init__` 里延迟 import**，
  没有提到模块顶层（理由见「圆角按钮的兼容契约」：顶层不 import tkinter，
  `--version` / `--selftest` 才不必加载 Tk）。
- ⚠️ `CARD_PAD_X` / `CARD_PAD_Y` / `CARD_R` 放成了**类属性**而不是 `__init__` 里的局部量 ——
  `card()` 的默认参数要在**类定义时**求值，那一刻还没有 `self`。
- 🔴 拆完之后 `sec()` 的报错文案指向 `LoginApp.SEC_STYLE`（原来是「run_gui 的 SEC_STYLE」）。
  有测试断言这句话时记得同步。
- ⚠️ **改竖向间距必须重新标定 `geometry_test.CONTENT_H`**：窗口高 = 内容高。
  两个都跑：`python content_h_probe.py`（本机真实状态）、
  `python content_h_matrix.py`（各状态矩阵）。
  不重新量的后果是这个测试拿旧数字自娱自乐。
- 🔴 **`CONTENT_H` 不是「唯一真值」，是一个随运行状态变的样本**（2026-10-04 实测）：

  | 状态 | `reqh`（圆角化前 → 后） |
  |---|---|
  | 0 账号 | 814 → **811** |
  | 1 账号（行内只有学号） | 807 → **804** |
  | 1 账号（带徽标 + 上次成功） | 830 → **827** |
  | 1 账号 + 自启警告 | 853 → **850** |
  | 2 账号 | 897 → **894** |
  | 2 账号 + 自启警告 | 943 → **940** |
  | 6 账号 | 1165 → **1162** |

  账号数、账号行里有没有「上次成功」、自启警告显不显示，都会改这个数。
  本文档上一版写的「1 账号无警告 814 / 2 账号 936」**两个都是错的** ——
  814 其实是 0 账号的值，2 账号是 897。所以 `geometry_test.py` 里除了
  `CONTENT_H`（默认状态样本）还多了一个 `MEASURED_HEIGHTS`，把整个范围都测一遍
  —— 只测一个魔数，等于没测到「这个数会变」这件事。

  **2026-10-04 界面圆角化那一轮，全档整齐 −3**（每档都是 830→827 这个差值）。
  「整齐」这件事本身是个好信号：说明圆角卡片改造是**几何中性**的，那 −3 来自
  另外三处刻意调整（列表容器去掉 1px 方角描边等），不是某个卡片被悄悄撑高/压扁。
  以后哪一轮改完发现各档的差值**不一致**，先去查那几张受影响的卡片，别直接改表。

## 学校参数：常量保留、只加一层访问器（2026-10-05）

`campus_login.py` 顶部有一节「学校参数：换学校只改 config.json」，里面是
`SCHOOL_KEYS`（八个可覆盖的键）。**换学校不用改源码**，写进数据目录的
`config.json` 就行（README 的「适配其它学校」一节是给用户看的同一件事）。

实现上刻意**没有**把常量搬进一个大字典，而是：

| 常量（留在原地，带自己的「为什么」注释） | 访问器（读 config 覆盖，退回常量） |
|---|---|
| `DEFAULTS`（`portalHost` / `wlanAcIp` / `wlanAcName` / `timeoutSec`） | `load_config()` |
| `DEFAULT_CAMPUS_SUBNETS` | `campus_subnets()` |
| `TRIGGER_URL` | `trigger_url()` |
| `CHECK_TARGETS` | `check_targets()` |
| `LOGIN_EXTRA` | `login_extra()` |

为什么这么拆，而不是「一个 `PORTAL_PROFILE` 大字典」：

1. **常量旁边那些注释是资产**。`CHECK_TARGETS` 上那段「为什么不是一个地址就够」
   （首次 DNS 7.4 秒）、`LOGIN_EXTRA` 上那段「以前三处各抄一份」——
   搬进一个远离使用现场的大字典，等于把它们流放了。项目最怕的正是「注释与代码脱节」。
2. **测试直接引用常量**（`net_check_test.py` 用 `C.CHECK_TARGETS[0][0]`、
   `form_drift_test.py` 用 `C.LOGIN_EXTRA`）。搬走就要改一堆测试，
   收益却只是「看起来整齐」。

🔴 **取值一律走访问器**。直接引用常量的话 config.json 改了它也不跟着变，
于是「配置看着写对了、实际压根没生效」—— 这正是本项目最警惕的那类静默错误
（`campus_subnets()` 从 2026-09-19 就是这么做的，另外三个是 2026-10-05 补齐）。

几个刻意的设计决定：

- **`loginExtra` 是浅合并，不是整体替换**。换学校时多数字段（`scheme` / `loginType` /
  `pageid`…）其实一样，逼用户抄全 20 个键只会抄漏。
  ⚠️ 已知限制：**没法靠配置删掉一个默认字段**（值写 `null` 会被 `load_config` 丢掉）。
- **`checkTargets` 是整体替换**。它是「一张检测表」，追加语义反而容易让人算不清最终有几条。
- **写坏了必须退回默认，不能把程序弄瘸**。`[]` / 类型不对 / 空串 / 只有坏元素，
  一律退回默认 —— 否则「测不出网」会被误判成「网络真的不通」，用户上不了网还查不出原因。
- **访问器都收一个可选的已读好的 `cfg`**，免得一次登录里把同一个文件读三遍。

回归靠 `school_profile_test.py`（43 项）。它有三段是**故意**这么设计的：

1. 无 config 时取值与默认常量**逐字相同**（证明没有悄悄改行为）；
2. 有 config 时**真的跟着变**，且 `auth_form` / `logout_form` / `url_parameter`
   这些**真正发出去的东西**也变了（只验访问器等于自嗨）；
3. **单键覆盖 → 清掉 → 必须变回来**，逐个键走一遍。第 3 段是防
   「往 `SCHOOL_KEYS` 加了个键，但忘了写访问器」—— 那时它会红。

## 站点域名：为什么**保留多份**、只加测试（2026-10-05）

发布站域名 `hobson2233.dpdns.org` 出现在 **7 个文件**里，各有各的理由：

| 文件 | 为什么这里有 |
|---|---|
| `repo/updater.py` | 拼更新清单地址（`MANIFEST_URL`） |
| `repo/campus_login.py` | 内置「使用说明」正文里写给用户看 |
| `repo/help_check.py` | 打包门槛核对 exe 里那份说明 |
| `make_site.py` | 站点根地址 + 页面文案 |
| `make_manifest.py` | 清单里的下载地址 |
| `deploy/deploy.py` | 发布后校验用的地址 |
| `wrangler.toml` | 配置注释里提醒「改 Worker 名会让这个域名失联」 |

以前只在文档里写了一句「三处必须同源」—— 而实际是 7 个文件，全靠人记。

🔴 **为什么不干脆合并成一处**：`repo/` 是要被**单独 clone / 单独打包**的
（PyInstaller onefile），`updater.py` 不能去 import 工作区根目录的文件 ——
合并会把 `repo/` 变成「离开工作区就跑不起来」。所以这里的选择是
**保留各自的副本，用测试守住一致性**，比强行合并便宜得多，也不动打包结构。

`domain_drift_test.py` 就是这个测试（已接进 `build.py`，**放在打包之前** ——
它只读源码，坏了立刻返回，不必等 PyInstaller 跑完才发现）：

- 金标准 = 「**所有提到它的地方说的是同一个域名**」，不是某个理想值。
  真要换域名时每一处都改，测试自然绿；只改一处会红 —— 那正是它存在的意义。
- 只认 `SUFFIX = "dpdns.org"` 这个后缀，不去猜「哪些字符串像域名」（那会误伤 `github.com`）。
  换域名时**连这个常量一起改**，测试会因为「一处都找不到」而报错，不会静默通过。
- 带阳性 + 阴性对照（`--selftest`）：注入第二个域名必须报「不止一个域名」、
  把某个来源清空必须报「一处都不提」、原样扫描必须零问题。

## 站点：本地预览与截图

站点文件由工作区根目录的 `make_site.py` **生成**（`site/*.html` 是产物，直接改会被下次构建覆盖），
所以要改站点就改 `make_site.py` 里的 `CSS` / `build_index_html()` 等。

| 脚本（工作区根目录） | 作用 |
|---|---|
| `preview_site.py` | 本地起一个服务看 `site/`，**复刻 Workers 的路由**（无扩展名是规范 URL、`/x.html` 307 跳 `/x`、`_headers` 按路径匹配、未命中渲染 404.html 且状态码是 404）。不发布。 |
| `refresh_screenshot.py` | 重新生成首页那张界面截图（跑 `ui_shot.py` 再拷进 `site_static/screenshot.png`）。**改了界面就必须跑**。 |

⚠️ 首页截图是**界面一改就会过期**的静态资源，而它过期时没有任何症状。
`make_site.py` 会核对两件事并打警告：图片真实尺寸 == `SCREENSHOT_SIZE`、
截图不比 `campus_login.py` 旧。那个 mtime 检查会误报（全新 clone 之后必报一次），
所以别把它当保证 —— 改完界面就顺手跑 `refresh_screenshot.py`。

⚠️ **`SCREENSHOT_SIZE` 只有一处定义**：HTML 里 `<img>` 的 `width/height` 是
`%(shot_w)s` / `%(shot_h)s` 插值进去的（2026-10-04 之前是写死的字面量，
界面圆角化让主窗口矮了 3px，那一处就漂了）。**别再往 HTML 里写死这两个数。**
尺寸会跟着主窗口高度走 —— 改界面竖向间距就会改它，`refresh_screenshot.py`
跑完看一眼新尺寸，不一致时改 `SCREENSHOT_SIZE` 一处即可。

⚠️ `preview_site.py` 没有 CDN 缓存层，本地看到的一定是最新的 ——
**别拿它判断线上缓存刷没刷**。

## 发布

改完代码要发新版本时，流程见工作区根目录的 `GitHub发布操作指南.md`。

⚠️ **`/dl/*` 是 immutable 长缓存，所以同一个版本号不能换字节。**
改完代码要发布时**先升 `VERSION`**，否则 `make_site.py` 会报「归档冲突」拒绝出包
（这条拦截是故意的：让「版本号没升就重新打包」在发布前就炸掉，而不是发出去才发现）。

⚠️ **`git push` 走不通时的兜底**（0.3.5 发布时实测 `github.com` 挂了、
`api.github.com` 却是通的）：可以用工作区根目录的 `api_push.py` 走 Git Data API 推送。
但它有两个副作用，**推完必须回读核对**：
1. **sha 必变**（GitHub 会去掉提交消息结尾的换行）→ 本地与远端历史分叉；
2. **署名会变成令牌持有者的账号身份**（不带 `author`/`committer` 时）→ 灰头像、
   不计贡献图，还会把个人邮箱写进公开仓库。

`api_push.py` 目前**没有**传 `author`/`committer`。用它推完要么按上面的方式修署名
（`probes/fix_commit_author.py`），要么给它补上显式署名。
本地/远端重新对齐用 `probes/reconcile_local_ref.py`（逐字节重建同一个 commit 对象）。
