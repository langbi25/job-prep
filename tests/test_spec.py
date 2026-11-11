"""P5 业务线 spec 的测试：加载器行为 + 配置/代码/数据三方一致性。

核心立场：spec 化的价值不在「有个 YAML」，而在**漂移可被测试拦住**——
评测数据里冒出 spec 没声明的 direction、工具 schema 的 enum 与 spec 不一致、
spec 指向的文件不存在，这三类漂移以前要靠人工对六个文件，现在测试一条命令。

测试零 LLM、零后端依赖；改 YAML 后记得 spec.reload()。
"""
import json
import sys
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core import config, spec  # noqa: E402
from app.services import harness  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ---------- 1. 加载器基本行为 ----------
def test_loader_basics():
    print("[1] loader basics")
    lines = spec.lines()
    check("lines 非空", len(lines) >= 1, str(lines))
    check("按 order 排序", lines == ("backend", "bigdata", "agent"), str(lines))
    check("enum 是 list", isinstance(spec.enum(), list))
    check("is_valid", spec.is_valid("backend") and not spec.is_valid("nope"))
    check("default 已声明", spec.is_valid(spec.default()))
    check("label 命中", spec.label("backend") == "后端")
    check("label 未知回退", spec.label("cs", fallback="基础") == "基础"
          and spec.label("_misc") == "_misc")


# ---------- 2. 与 config / 工具 schema 的一致性 ----------
def test_consistency_with_code():
    print("[2] consistency with config & tool schemas")
    check("config.DIRECTIONS == spec.lines()",
          config.DIRECTIONS == spec.lines(), str(config.DIRECTIONS))
    check("config.DOMAINS = DIRECTIONS + cs",
          config.DOMAINS == spec.lines() + ("cs",))
    check("config.DEFAULT_DIRECTION == spec.default()",
          config.DEFAULT_DIRECTION == spec.default())

    # harness 工具 schema 里的 direction enum 必须与 spec 一致
    enums = []
    for t in harness.TOOLS:
        props = (t.get("function", {}).get("parameters", {}).get("properties") or {})
        if "direction" in props and "enum" in props["direction"]:
            enums.append(props["direction"]["enum"])
    check("harness 里有 direction enum 的工具存在", len(enums) >= 1)
    check("harness enum == spec.enum()",
          all(e == spec.enum() for e in enums), str(enums))

    from app.services import agent_tools
    enums2 = []
    for t in agent_tools.SCHEMAS:
        props = (t.get("function", {}).get("parameters", {}).get("properties") or {})
        if "direction" in props and "enum" in props["direction"]:
            enums2.append(props["direction"]["enum"])
    check("agent_tools enum == spec.enum()",
          all(e == spec.enum() for e in enums2), str(enums2))


# ---------- 3. eval_path 与 validate ----------
def test_eval_paths_and_validate():
    print("[3] eval_path & validate")
    for name in ("golden_answers", "retrieval_cases", "tool_gap_cases",
                 "extract_judge_cases", "agent_tasks", "thresholds"):
        p = spec.eval_path(name)
        check(f"eval_path({name}) 存在", p.exists(), str(p))
    try:
        spec.eval_path("no_such_file")
        check("未知 eval 名报 KeyError", False)
    except KeyError:
        check("未知 eval 名报 KeyError", True)

    problems = spec.validate()
    check("validate 通过", problems == [], str(problems))


# ---------- 4. 反向漂移检测（临时 YAML 注入） ----------
def test_validate_catches_drift():
    print("[4] validate catches drift")
    orig_text = spec.SPEC_PATH.read_text(encoding="utf-8")
    tdir = tempfile.TemporaryDirectory()
    tpath = pathlib.Path(tdir.name) / "business_lines.yaml"
    # 注入一个数据里没有的业务线 + 一个不存在的 eval 文件 → validate 应报出来
    bad = orig_text.replace("lines:", "lines:") \
        .replace("defaults:", "defaults:\n  evals:\n    bogus: evals/does_not_exist.jsonl")
    # 上面 defaults 已有 evals，会变成两个 evals 键（YAML 后者覆盖）——换成直接改行内容
    bad = orig_text.replace("    thresholds: evals/thresholds.json",
                            "    thresholds: evals/thresholds.json\n    bogus: evals/does_not_exist.jsonl")
    bad = bad.replace("  backend:\n    label: 后端\n    order: 1",
                      "  backend:\n    label: 后端\n    order: 1\n  ghost:\n    label: 幽灵线\n    order: 9")
    tpath.write_text(bad, encoding="utf-8")

    real_path = spec.SPEC_PATH
    try:
        spec.SPEC_PATH = tpath
        spec.reload()
        problems = spec.validate()
        check("不存在的 eval 文件被报出",
              any("does_not_exist" in p for p in problems), str(problems))
        # ghost 线本身合法（order 不冲突），但 defaults.direction 仍有效 → 不额外报错
        check("合法新增线不误报", not any("ghost" in p for p in problems), str(problems))

        # order 冲突检测
        bad2 = bad.replace("order: 9", "order: 1")
        tpath.write_text(bad2, encoding="utf-8")
        spec.reload()               # YAML 改了必须清缓存，否则 validate 读到旧快照
        problems2 = spec.validate()
        check("order 重复被报出", any("order 重复" in p for p in problems2), str(problems2))
    finally:
        spec.SPEC_PATH = real_path
        spec.reload()
        tdir.cleanup()
    check("reload 后恢复", spec.validate() == [])


# ---------- 5. 评测数据 → spec 反向校验（agent_tasks 的 direction） ----------
def test_eval_data_directions_declared():
    print("[5] eval case directions declared")
    tasks = [json.loads(l) for l in
             spec.eval_path("agent_tasks").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    dirs = {t.get("direction") for t in tasks if t.get("direction")}
    unknown = dirs - set(spec.lines())
    check(f"agent_tasks 的 direction {dirs} 全部已在 spec 声明", not unknown, str(unknown))


if __name__ == "__main__":
    test_loader_basics()
    test_consistency_with_code()
    test_eval_paths_and_validate()
    test_validate_catches_drift()
    test_eval_data_directions_declared()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
