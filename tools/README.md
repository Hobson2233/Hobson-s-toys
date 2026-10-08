# tools/ —— 增量更新用的第三方二进制

这个目录里**只有两个文件是别人写的**，其余是说明。

| 文件 | 大小 | 干什么 |
|---|---|---|
| `hpatchz.exe` | 515072 字节 | **客户端**用：拿旧程序 + 补丁还原出新程序。会被 `build.py` 用 `--add-binary` 打进单文件 exe |
| `hdiffz.exe` | 1489920 字节 | **构建时**用：生成补丁。只在 `make_site.py` 里跑，不进 exe |

## 来源（可核对）

- 项目：[sisong/HDiffPatch](https://github.com/sisong/HDiffPatch)
- 版本：**v5.1.3**（2026-07-31 发布）
- 发布包：`hdiffpatch_v5.1.3_bin_windows64.zip`（960575 字节）
- 下载地址：`https://github.com/sisong/HDiffPatch/releases/download/v5.1.3/hdiffpatch_v5.1.3_bin_windows64.zip`
- 许可证：**MIT**（原文见同目录 `LICENSE-HDiffPatch.txt`，Copyright (c) 2012-2025 housisong）

### 已核对过的哈希

2026-10-08 重新下载官方发布包、解开、与本目录的文件逐字节比对，**完全一致**：

| 文件 | sha256 |
|---|---|
| `hdiffz.exe` | `f5ed7ac622a2daf4a31cc21ffa8ea1717f92323e79ef5ae695c5c9238a282f52` |
| `hpatchz.exe` | `9703c694b5955c576d9f0e26e98b60941f0bbb53b382b1f1988d75d461e580cb` |

> 为什么要记下来：这两个文件是**可执行二进制**，直接进仓库就等于进发布链路。
> 哪天要换版本，先按上面的地址重新下一份、比对哈希，确认没被动过手脚再替换。
> 「从官网下载的」不是证据，**哈希对得上**才是。

## 为什么把二进制放进仓库

因为 `build.py` 和 `make_site.py` 都要用它。放进仓库 = **构建可复现**：
换台电脑 clone 下来就能打包，不需要联网去下工具，也不会因为「官网哪天挂了」
或「下到的是被替换过的版本」而打出一个不一样的包。

代价是仓库多了约 2 MB。对一个每次更新要省 10 MB 的机制来说，这个交换是划算的。

## 为什么不用 Python 库

评估过 `detools`（BSD-2）和几个同类项目，结论是**都不行**：

- `detools` 在 PyPI 上**只发源码包、没有任何 wheel**，安装要 MSVC 编译工具链
  （它的 `suffix_array` / `hdiffpatch` 路径是 C 扩展）。用户装不了，构建机也得配环境。
- 其余几个要么已归档停更（`PyUpdater`）、要么没有许可证（`jedisct1/ed25519.py`）、
  要么拖一大堆依赖把 exe 撑大（`tufup` 拖 `cryptography`，涨 3~10 MB）。

HDiffPatch 官方直接发 Windows 二进制，MIT，零依赖 —— 代价只有一次性的 515 KB。

> ⚠️ **`detools` 那条结论来自实测推翻**：一开始是子代理报的「纯 Python、体积≈0、
> 强烈推荐」，实际 `pip install` 直接失败。**子代理的结论必须自己跑一遍再说。**
