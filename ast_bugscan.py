#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AST 级 bug 扫描器 —— 找 pyflakes 不查、但会静默出错的写法。

检查项（每条都是"不报错但行为不对"的类型）：
  1. 重复的字典键          -> 后面的静默覆盖前面的，写错字看不出来
  2. 重复的模块级函数/类名  -> 后面的静默覆盖前面的，旧实现变成死代码
  3. `is` 和字面量比较      -> `x is 1` 恒为 False（CPython 只缓存小整数，别赌）
  4. 可变默认参数           -> `def f(a=[])` 跨调用共享同一个 list
  5. `except X: pass` 空体  -> 异常被完全吞掉，出错时无声无息
  6. finally 里的 return    -> 会吃掉异常和 return 值
  7. `if x == 1 or 2` 型    -> 恒真，条件失效
  8. 赋值后从未使用的局部变量 -> 常常是"算了但忘了用"的逻辑漏
  9. 参数默认值里的调用      -> 只在定义时求值一次（时间戳/随机数会冻结）

用法: python ast_bugscan.py [文件或目录 ...]     退出码 = 发现的问题数（>0 即非零）
"""
import ast
import os
import sys

# 第 8 项容易误报（故意留的调试变量、解包占位），只报"名字看起来有意义"的
_IGNORE_NAMES = {"_", "__", "s", "e", "ex", "exc", "v", "k", "i", "j", "n", "x", "y"}

# 建议级（不计入退出码，除非 --strict）。
# EMPTYEXC 单独降级的原因：项目里大量 `except Exception: pass` 是**有意的兜底**
# （退出前 flush、写日志失败、外观设置失败……），逐条判过，多数不该改。
# 但它是"排查时最该先看的一类"，所以仍然报出来，只是不拦人。
WARN_CODES = {"EMPTYEXC"}


class Scan(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.issues = []

    def report(self, node, code, msg):
        self.issues.append((getattr(node, "lineno", 0), code, msg))

    # 1. 重复字典键
    def visit_Dict(self, node):
        seen = {}
        for k in node.keys:
            if isinstance(k, ast.Constant):
                key = (type(k.value).__name__, k.value)
                if key in seen:
                    self.report(k, "DUPKEY",
                                "重复的字典键 %r（第 %d 行已出现过，这里会静默覆盖）"
                                % (k.value, seen[key]))
                else:
                    seen[key] = k.lineno
        self.generic_visit(node)

    # 2. 重复的模块级定义
    def visit_Module(self, node):
        seen = {}
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if stmt.name in seen:
                    self.report(stmt, "DUPDEF",
                                "重复定义 %s（第 %d 行已定义过，这里会静默覆盖）"
                                % (stmt.name, seen[stmt.name]))
                else:
                    seen[stmt.name] = stmt.lineno
        self.generic_visit(node)

    # 3. is / is not 与字面量比较
    def visit_Compare(self, node):
        for op, cmp in zip(node.ops, node.comparators):
            if isinstance(op, (ast.Is, ast.IsNot)):
                if isinstance(cmp, ast.Constant) and not (
                        cmp.value is None or cmp.value is True or cmp.value is False):
                    self.report(node, "ISLITERAL",
                                "用 `is` 和字面量 %r 比较，应改用 `==`" % (cmp.value,))
        self.generic_visit(node)

    # 4. 可变默认参数
    def _check_defaults(self, node):
        for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.report(d, "MUTDEF",
                            "%s 的可变默认参数跨调用共享同一个对象，应改用 None + 内部初始化"
                            % node.name)
            # 9. 默认值里的调用
            elif isinstance(d, ast.Call):
                self.report(d, "CALLDEF",
                            "%s 的默认值在定义时求值一次（不是每次调用），若含时间/随机数会冻结"
                            % node.name)

    def visit_FunctionDef(self, node):
        self._check_defaults(node)
        self._check_shadow(node)
        self._check_unused_locals(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_defaults(node)
        self.generic_visit(node)

    # 5 / 6. 空 except 体、finally 里 return
    def visit_Try(self, node):
        for h in node.handlers:
            body = h.body
            # 去掉 docstring 后看是否为空
            real = [s for s in body if not (isinstance(s, ast.Expr)
                                            and isinstance(s.value, ast.Constant))]
            if not real or (len(real) == 1 and isinstance(real[0], ast.Pass)):
                self.report(h, "EMPTYEXC",
                            "except 体是空的（异常被完全吞掉），至少记一行日志")
        for s in node.finalbody:
            for sub in ast.walk(s):
                if isinstance(sub, ast.Return):
                    self.report(sub, "FINRET", "finally 里的 return 会吃掉异常和返回值")
        self.generic_visit(node)

    # 7. `if x == 1 or 2`  —— 只报"第一个操作数是比较、后面跟常量"这一种真错写法。
    #    `y = x or ''` 这种是合法惯用法（回退默认值），不能报。
    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or) and node.values:
            if isinstance(node.values[0], ast.Compare):
                for v in node.values[1:]:
                    if isinstance(v, ast.Constant) and not isinstance(v.value, bool):
                        self.report(v, "ORCONST",
                                    "`or %r` 恒为真，前面的比较等于失效" % (v.value,))
        self.generic_visit(node)

    # 8. 赋值后未使用的局部变量
    #    只看"单个名字 = 表达式"，不看元组解包（`rc, out, _ = ...` 里不用 rc 是惯例）；
    #    声明了 global/nonlocal 的名字不算局部变量，跳过（否则误报）
    def _check_unused_locals(self, node):
        assigned = {}
        used = set()
        declared_global = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global) or isinstance(sub, ast.Nonlocal):
                declared_global.update(sub.names)
            elif isinstance(sub, ast.Name):
                if isinstance(sub.ctx, ast.Store):
                    assigned.setdefault(sub.id, sub.lineno)
                else:
                    used.add(sub.id)
            elif isinstance(sub, ast.arg):
                used.add(sub.arg)
            elif isinstance(sub, ast.Assign):
                # 记录"裸名字赋值"的行号，元组解包不算
                if len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    assigned[sub.targets[0].id] = sub.targets[0].lineno
        simple = set(assigned)
        # 把所有 Store 里非裸赋值的名字剔掉。
        # 注意范围要够宽：不只是 `a, b = ...` 这种 Assign 解包，
        # `for root, dirs, files in os.walk(...)`（循环解包）、
        # `with open(p) as f, open(q) as g:`、推导式目标，都同属"解包/绑定"，
        # 其中某个名字用不到是极常见的写法，不算 bug。
        # （最初只判了 Assign，结果把 os.walk 的 `dirs` 误报成未使用 —— 已修。）
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if not isinstance(t, ast.Name):
                        for n in ast.walk(t):
                            if isinstance(n, ast.Name):
                                simple.discard(n.id)
            elif isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)):
                for n in ast.walk(sub.target):
                    if isinstance(n, ast.Name):
                        simple.discard(n.id)
            elif isinstance(sub, ast.withitem):
                if sub.optional_vars is not None:
                    for n in ast.walk(sub.optional_vars):
                        if isinstance(n, ast.Name):
                            simple.discard(n.id)
        for name in sorted(simple, key=lambda n: assigned[n]):
            if (name in used or name in _IGNORE_NAMES or name.startswith("_")
                    or name in declared_global):
                continue
            self.report(node, "UNUSEDVAR",
                        "%s() 里第 %d 行赋值的局部变量 `%s` 之后从未使用"
                        % (node.name, assigned[name], name))

    # 内置名遮蔽
    def _check_shadow(self, node):
        builtins_used = {"list", "dict", "set", "str", "int", "type", "id", "input",
                         "open", "len", "sum", "min", "max", "filter", "map", "format",
                         "bytes", "hash", "next", "vars", "dir", "abs", "all", "any"}
        for a in node.args.args:
            if a.arg in builtins_used:
                self.report(a, "SHADOW",
                            "%s() 的参数 `%s` 遮蔽了内置函数名" % (node.name, a.arg))


def scan_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print("!! 读不了 %s: %s" % (path, e))
        return []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [(e.lineno or 0, "SYNTAX", "语法错误: %s" % e.msg)]
    s = Scan(path)
    s.visit(tree)
    return sorted(s.issues)


def iter_py(targets):
    for t in targets:
        if os.path.isfile(t):
            yield t
        else:
            for root, dirs, files in os.walk(t):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "build2",
                                                        "dist2", "ui_shots")]
                for fn in sorted(files):
                    if fn.endswith(".py"):
                        yield os.path.join(root, fn)


def self_test():
    """阳性对照：故意写一段有全部 8 类 bug 的代码，看扫描器抓不抓得到。"""
    # 对照代码里故意写了 finally+return，Python 会抛 SyntaxWarning。
    # 压掉它，否则每跑一次都刷一行，容易被误当成"工具坏了"。
    import warnings
    warnings.simplefilter("ignore", SyntaxWarning)
    bad = '''
def f(a=[]):
    if a == 1 or 2:
        pass
    computed_value = 5
    return a is 1

def g():
    try:
        pass
    except Exception:
        pass
    finally:
        return 1

d = {"k": 1, "k": 2}

def f():
    return 0
'''
    tree = ast.parse(bad)
    s = Scan("<selftest>")
    s.visit(tree)
    codes = {c for _, c, _ in s.issues}
    want = {"DUPKEY", "DUPDEF", "ISLITERAL", "MUTDEF", "EMPTYEXC", "FINRET",
            "ORCONST", "UNUSEDVAR"}
    missing = want - codes
    if missing:
        print("!! 阳性对照失败，漏检: %s" % ", ".join(sorted(missing)))
        return 1
    print("[阳性对照] 8 类 bug 全部命中: %s" % ", ".join(sorted(codes)))

    # 阴性对照：这些写法看着像 bug 但完全合法，报了就是误报
    good = '''
import os

SETTING = None

def configure():
    global SETTING
    SETTING = 1

def pick(value):
    rc, out, _ = (0, "ok", None)
    label = value or "默认值"
    if value is None or value is True or value is False:
        pass
    return label

def dump():
    for name, label in [("a", "A")]:
        print(name, label)

def walk_all(root):
    # 循环解包里 subdirs 用不到，不算 bug
    for cur, subdirs, names in os.walk(root):
        print(cur, names)

def open_two(a, b):
    # with 多目标绑定，只用一个也不算 bug
    with open(a) as fa, open(b) as fb:
        return fa.read()
'''
    s2 = Scan("<negative>")
    s2.visit(ast.parse(good))
    if s2.issues:
        print("!! 阴性对照失败，误报了合法写法：")
        for ln, code, msg in s2.issues:
            print("   %d: [%s] %s" % (ln, code, msg))
        return 1
    print("[阴性对照] 合法写法（global 赋值 / 元组解包 / or 默认值 / is None）零误报")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        return self_test()
    if not args:
        args = [os.path.dirname(os.path.abspath(__file__))]
    strict = "--strict" in sys.argv
    n_err = 0
    n_warn = 0
    files = 0
    for path in iter_py(args):
        issues = scan_file(path)
        if issues:
            rel = os.path.relpath(path)
            print("\n== %s ==" % rel)
            for ln, code, msg in issues:
                tag = "建议" if code in WARN_CODES else "错误"
                print("  %s:%d: [%s/%s] %s" % (rel, ln, tag, code, msg))
                if code in WARN_CODES:
                    n_warn += 1
                else:
                    n_err += 1
        files += 1
    print("\n扫描 %d 个文件：错误 %d 个，建议 %d 个。" % (files, n_err, n_warn))
    if n_warn and not strict:
        print("（建议级不计入退出码；要看全部就用 --strict）")
    counted = n_err + (n_warn if strict else 0)
    return 0 if counted == 0 else min(counted, 100)


if __name__ == "__main__":
    sys.exit(main())
