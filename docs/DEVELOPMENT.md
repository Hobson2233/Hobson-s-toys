# 开发说明

面向本项目开发者。用户使用说明见 [README](../README.md)。

## 项目结构

```
campus_login.py         主程序（单文件，无参=GUI；命令行开关见 `SWITCH_HELP`，或用 --help 打印）
paths.py                统一的路径解析（不要在脚本里写死绝对路径）
proc_tree.py            进程树管理：Job Object + 看门狗 + _MEI 残留清理
build.py                构建入口，带四道门槛
savepoint.py            本地存档：改代码前打快照，搞砸了能回退
ast_bugscan.py          AST 级 bug 扫描（自查用，带阳性/阴性对照）
stack_probe.py          卡住时打印所有线程堆栈（排障用）
local_secrets.example.py  本地凭据模板（复制成 local_secrets.py 填真值，已被 gitignore）
docs/                   README 用的界面截图与本文件
```

## 测试与诊断脚本

| 脚本 | 作用 | 能不能当门槛 |
|---|---|---|
| `leak_check.py` | 隐私检查：exe 里有没有夹带账号密码 | 是门槛 |
| `portability_check.py` | 可移植性：换台电脑能不能跑 | 是门槛 |
| `secret_scan.py` | **提交前**扫凭据 / 本机绝对路径 | 提交前跑（`--staged`） |
| `help_check.py` | 内置使用说明确实进了 exe | 否（可以加进 build.py） |
| `wiring_gate_test.py` | 验「接线自检」这道门槛的报警链路本身通不通 | 否 |
| `verify_exe.py` | 只验不打包；可传 exe 路径，用来拿旧版本做阳性对照 | 否 |
| `pwd_test.py` | 密码框显示规则（纯函数） | 否 |
| `poll_test.py` | 界面轮询队列：出错要不要记日志、会不会中断整批 | 否 |
| `pwd_gui_test.py` | 密码框真实窗口绑定 | 否（要真桌面） |
| `geometry_test.py` | 9 种屏幕尺寸下窗口不超出屏幕 | 否 |
| `proc_tree_test.py` | 进程树与看门狗，含「故意复现孤儿进程」 | 否 |
| `autostart_test.py` | 开机自启的增删改查（**计划任务 + `.lnk` 两条路**）、`.lnk` 参数读法、`autostart_stale` / `autostart_outdated`、**升级迁移不被构建产物污染** | 否 |
| `guard_test.py` | 守护判断表、守护标记自愈、用户主动退出的避让、`do_auto` 重试、`.lnk` 参数读法 | 否 |
| `network_state_test.py` | 校园网判定 / MAC 校验 / `network_state` 四态 / 门户 MAC 替换 | 否 |
| `auto_timing_probe.py` | 把每次 `--auto` 的耗时按阶段拆开（定位「自启慢」慢在哪一段） | 否（诊断用） |
| `ui_shot.py` / `layout_probe.py` | 截图 / 量布局（要真桌面） | 否 |
| `png_zoom.py` | 裁切放大 PNG 局部、数字形个数（核对截图里画了什么） | 否 |
| `savepoint.py` | 本地存档：改代码前打快照，搞砸了能整体回退 | 否（改代码前用） |
| `ast_bugscan.py` | AST 级 bug 扫描：重复字典键 / 重复定义 / `is` 比字面量 / 可变默认参数 / 空 except / `finally` 里 return / `if x==1 or 2` / 赋值后未用 | 否（自查用，带阳性+阴性对照） |
| `stack_probe.py` | 跑到卡住时把**所有线程**的堆栈打出来（`faulthandler`） | 否（排障用） |

跑测试要用**带 PyInstaller 的解释器**（`leak_check` / `help_check` / `portability_check` 依赖它）。
`autostart_test.py` / `guard_test.py` 要写 `.lnk`，需要 **pylnk3**，只有 `envs/gui314` 里有 ——
缺依赖时 `guard_test.py` 会明确报错退出（码 3），**不会静默跳过**那几条断言。
`autostart_test.py` 会把 `task_state` / `task_create` / `task_delete` 换成桩（**不碰真实的计划任务**），
但它**真的会读写启动文件夹里的 `.lnk`** —— 跑完记得核一眼自启状态。

构建门槛的细节：`build.py` 的第 2 步（冒烟测试）里，密码框接线自检失败会输出 `GUI_FAIL`
标记，`build.py` 见到该标记即判失败；`wiring_gate_test.py` 专门验这条报警链路本身通不通。
`--guitest` 只作参考（建 Tk 窗口在无人值守会话里不稳定），不作为判据。

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

## 发布

改完代码要发新版本时，流程见工作区根目录的 `GitHub发布操作指南.md`。

⚠️ **`git push` 走不通时的兜底**（0.3.5 发布时实测 `github.com` 挂了、
`api.github.com` 却是通的）：可以用工作区根目录的 `api_push.py` 走 Git Data API 推送。
但它有两个副作用，**推完必须回读核对**：
1. **sha 必变**（GitHub 会去掉提交消息结尾的换行）→ 本地与远端历史分叉；
2. **署名会变成令牌持有者的账号身份**（不带 `author`/`committer` 时）→ 灰头像、
   不计贡献图，还会把个人邮箱写进公开仓库。

`api_push.py` 目前**没有**传 `author`/`committer`。用它推完要么按上面的方式修署名
（`probes/fix_commit_author.py`），要么给它补上显式署名。
本地/远端重新对齐用 `probes/reconcile_local_ref.py`（逐字节重建同一个 commit 对象）。
