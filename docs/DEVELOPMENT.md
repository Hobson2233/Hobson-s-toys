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
| `verify_exe.py` | 只验不打包 | 否 |
| `pwd_test.py` | 密码框显示规则（纯函数） | 否 |
| `poll_test.py` | 界面轮询队列：出错要不要记日志、会不会中断整批 | 否 |
| `pwd_gui_test.py` | 密码框真实窗口绑定 | 否（要真桌面） |
| `geometry_test.py` | 9 种屏幕尺寸下窗口不超出屏幕 | 否 |
| `proc_tree_test.py` | 进程树与看门狗，含「故意复现孤儿进程」 | 否 |
| `autostart_test.py` | 开机自启的增删改查、`.lnk` 参数读法、`autostart_stale` / `autostart_outdated` | 否 |
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

## 发布

改完代码要发新版本时，流程见工作区根目录的 `GitHub发布操作指南.md`。
