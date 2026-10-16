# -*- coding: utf-8 -*-
"""stdout 健壮性：任何形态的 stdout 下，`print` 都不许抛异常。

## 起因（2026-10-16，被构建门槛抓到）

`--selftest` 的 `print(text)` 在**写 selftest.txt 之前**（campus_login.py 里
紧挨着的两行），而 `text` 里有一行「密码加密: ⚠️ 还有 1/2 条是明文」。
当 stdout 被重定向时（`build.py` 的冒烟测试接的是 DEVNULL），`sys.stdout`
**不是 None**、编码是 locale（中文 Windows = cp936）⇒ `⚠️` 编不进去
⇒ `UnicodeEncodeError`。

放大成灾难的是下一步：`--windowed` 的 exe 遇到未捕获异常会弹一个
**等人点 Close** 的错误框 ⇒ 进程挂在那里 ⇒ `build.py` 45 秒超时判失败，
而它报出来的是「--selftest 超时 / 界面可能有问题」—— **完全指不到真原因**。
（0.5.6 时 selftest 的输出里没有非 GBK 字符，所以这个坑一直没暴露。）

## 这个测试钉四件事

  ① **阳性对照**：不修的话，gbk 流上 print `⚠️` **确实会抛** ——
     没有这一条，②里「不抛」可能只是因为「样本本来就没问题」。
  ② `_fix_std_streams()` 之后不抛，而且**中文照常写得出去**（没把编码改坏）。
  ③ stdout 是 None 时也不抛（双击 exe 的那种形态）。
  ④ **防空转**：直接扫 `--selftest` 那段源码，断言它的输出里**真的**存在
     GBK 表示不了的字符。将来谁把 `⚠️` 删了，这条会红 —— 否则前面几条
     会静悄悄退化成「测一个不存在的问题」。

⚠️ 本测试**不碰数据目录**（不写任何文件），所以不需要 `datasafe.sandbox()`。
   唯一的文件访问是读 campus_login.py 自己的源码。

用法：
    python stdout_safety_test.py
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import campus_login as C  # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print("  %s %-50s got=%r want=%r" % ("[OK]" if ok else "[!!]", name, got, want))


def check_true(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("[OK]" if cond else "[!!]", name, extra))


def section(t):
    print("\n--- %s ---" % t)


# 故意用**真实那一行**做样本（不是随便找个 emoji）：
# 它是触发这次故障的原句，改了这句就该有人想想这个测试还有没有意义。
SAMPLE = "密码加密: ⚠️ 还有 1/2 条是明文（下次保存会自动加密）"


def gbk_stream():
    """一个「被重定向到文件」的替身：cp936 编码 + strict（= 真实默认行为）。

    ⚠️ 必须是真的 `TextIOWrapper`：`StringIO` 不编码，永远测不出这个问题。
    """
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")


class _Swap(object):
    """临时替换 sys.stdout / sys.stderr，退出时无条件还原。"""

    def __init__(self, out=None):
        self.out = out

    def __enter__(self):
        self.old = (sys.stdout, sys.stderr)
        if self.out is not None:
            sys.stdout = self.out
        return self

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self.old
        return False


def test_positive_control():
    section("1. 阳性对照：不修的话确实会抛")
    s = gbk_stream()
    with _Swap(out=s):
        try:
            print(SAMPLE)
            raised = "（没抛）"
        except UnicodeEncodeError:
            raised = "UnicodeEncodeError"
        except Exception as e:                       # noqa: BLE001
            raised = type(e).__name__
    check("gbk 流上 print ⚠️ 抛 UnicodeEncodeError", raised, "UnicodeEncodeError")


def test_fixed_stream():
    section("2. _fix_std_streams() 之后：不抛，中文完好，编码没被改")
    s = gbk_stream()
    with _Swap(out=s):
        had, patched = C._fix_std_streams()
        try:
            print(SAMPLE)
            err = ""
        except Exception as e:                       # noqa: BLE001
            err = type(e).__name__
        try:
            sys.stdout.flush()
        except Exception:                            # noqa: BLE001
            pass
        raw = s.buffer.getvalue()
        enc = sys.stdout.encoding
    check("修复后不抛", err, "")
    check("had=True（本来就有 stdout）", had, True)
    check("patched=True（确实改过 errors）", patched, True)
    check("encoding 没被改掉", (enc or "").lower().replace("-", ""), "gbk")
    check_true("中文照常写得出去",
               "密码加密" in raw.decode("gbk", "replace"), raw[:40])


def test_none_stdout():
    section("3. stdout 是 None（双击 exe 那种形态）")
    old = (sys.stdout, sys.stderr)
    try:
        sys.stdout = None
        had, _ = C._fix_std_streams()
        try:
            print(SAMPLE)
            err = ""
        except Exception as e:                       # noqa: BLE001
            err = type(e).__name__
        got = sys.stdout
    finally:
        sys.stdout, sys.stderr = old
    check("had=False（本来没有 stdout）", had, False)
    check_true("stdout 被接上了，不再是 None", got is not None, "")
    check("print 不抛", err, "")


def test_non_reconfigurable():
    section("4. 替身流（没有 reconfigure）不许把函数搞崩")
    s = io.StringIO()
    with _Swap(out=s):
        try:
            C._fix_std_streams()
            err = ""
        except Exception as e:                       # noqa: BLE001
            err = type(e).__name__
        try:
            print("中文和 ⚠️ 都能进 StringIO")
            perr = ""
        except Exception as e:                       # noqa: BLE001
            perr = type(e).__name__
    check("对没有 reconfigure 的流不抛", err, "")
    check("StringIO 上 print 正常", perr, "")


def test_source_still_has_non_gbk():
    """防空转：`--selftest` 的输出里必须真的有 GBK 编不出的字符。

    没有这一条，将来谁把 `⚠️` 从 selftest 里删掉，上面几条就变成
    「测一个不存在的问题」—— 照样全绿，但什么也没保证。
    """
    section("5. 防空转：selftest 的输出里确实有非 GBK 字符")
    src_path = os.path.join(HERE, "campus_login.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    i = src.find('if "--selftest" in args')
    j = src.find("exit_now(0)", i)
    check_true("定位到了 selftest 分支", i > 0 and j > i, "i=%d j=%d" % (i, j))
    seg = src[i:j] if (i > 0 and j > i) else ""
    bad = sorted({ch for ch in seg if not _gbk_ok(ch)})
    check_true("那段源码里有 GBK 表示不了的字符", bool(bad),
               "找到 %d 种：%s" % (len(bad), "".join(bad)))


def _gbk_ok(ch):
    try:
        ch.encode("gbk")
        return True
    except Exception:                                # noqa: BLE001
        return False


def main():
    print("=" * 68)
    print("stdout 健壮性自检（gbk 重定向下 print 不许抛）")
    print("=" * 68)
    test_positive_control()
    test_fixed_stream()
    test_none_stdout()
    test_non_reconfigurable()
    test_source_still_has_non_gbk()
    print("\n" + "=" * 68)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  [!!] %s" % n)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
