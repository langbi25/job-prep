"""P6 复盘中心的测试：文档→考点→题目→审批卡全链路（注入假 LLM，零成本）。

关键不变量的测试点：
- 卡不直写：build_cards 产物必须是 agent_actions(pending) 的 create_card；
- 题目入池去重：与池中已有题面相同的不重复入池；
- 安全面：ingest_from_doc 拒绝项目根之外的路径；
- 失败落表：run_pipeline 中途出错，error 记入 retrospectives，已完成步骤保留。
"""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.core import llm  # noqa: E402
from app.core.db import get_conn, init_db  # noqa: E402
from app.services import retrospective_service as retro  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ---------- 假 LLM：按 prompt 特征分派 ----------
def fake_chat_parsed(system, user):
    if "面试官。用户会给你一篇复盘文档" in system:
        return {"points": [
            {"point": "为什么评测双写不走 Phoenix 的 run_experiment", "why": "考察对单一事实源的理解",
             "ask": "追问如果平台重跑会有什么问题", "topic": "评测架构"},
            {"point": "prompt 优化为什么以文件为事实源", "why": "考察状态管理", "ask": "追问回滚怎么做",
             "topic": "prompt 管理"},
            {"point": "短考点（凑数的）", "why": "w", "ask": "a", "topic": "t"},
        ]}
    if "根据考点清单出题" in system:
        return {"questions": [
            {"text": "你做评测双写时为什么不直接用 Phoenix 的 run_experiment？", "qtype": "项目深挖",
             "topic": "评测架构", "point": "评测双写"},
            {"text": "prompt 版本管理和代码版本管理有什么不同？你怎么做回滚的？", "qtype": "项目深挖",
             "topic": "prompt 管理", "point": "prompt 事实源"},
            {"text": "这题的 qtype 是非法值，应被矫正", "qtype": "不存在的题型", "topic": "x", "point": "y"},
        ]}
    if "知识卡片" in system or "知识卡" in system:
        return {"title": "【复盘】评测双写架构", "points": [" Phoenix 只做分析层不执行业务逻辑，结果单一出处可信",
                                                        " 双写失败不阻断本地门禁，观测不能拖垮主流程",
                                                        " sync=true 后有可见性窗口，read-after-write 要重试",
                                                        " 数据集内容 hash 幂等，重推零重复"],
                "pitfalls": ["把平台当执行引擎会形成第二执行路径"],
                "tags": ["评测", "可观测"], "reason": "复盘文档提炼"}
    return {}


CONTENT = ("# P6 复盘测试\n\n" + "我们在项目里建设了评测与归因闭环。" * 30)  # >200 字


def main():
    init_db()
    orig = llm.chat_parsed
    llm.chat_parsed = fake_chat_parsed
    try:
        # 1. 导入
        print("[1] ingest")
        try:
            retro.ingest("太短")
            check("短内容被拒绝", False)
        except ValueError:
            check("短内容被拒绝", True)
        ing = retro.ingest(CONTENT, title="自进化闭环复盘", source_path="docs/x.md")
        rid = ing["retro_id"]
        check("导入成功落表", rid.startswith("retro-") and ing["chars"] == len(CONTENT))
        check("重复导入产生新 id", retro.ingest(CONTENT)["retro_id"] != rid)

        # 2. 考点提炼
        print("[2] extract")
        ext = retro.extract_points(rid)
        check("提炼出考点（空 point 被过滤）", len(ext["points"]) == 3)
        row = get_conn().execute("SELECT status, points_json FROM retrospectives WHERE id=?", (rid,)).fetchone()
        check("status=extracted", row["status"] == "extracted")

        # 3. 建题入池
        print("[3] questions")
        # 先塞一道与假 LLM 输出相同的题，验证去重（测试可重复跑，先清掉历轮同文题）
        conn = get_conn()
        conn.execute("DELETE FROM question_pool WHERE text LIKE '%run_experiment%' OR text LIKE '%版本管理%' OR text LIKE '%qtype 是非法值%'")
        conn.execute("DELETE FROM agent_actions WHERE kind='create_card' AND status='pending' "
                     "AND payload_json LIKE '%【复盘】评测双写架构%'")
        conn.execute("INSERT INTO question_pool (id,text,aliases_json,qtype,topic,freq_count,first_seen_at,last_seen_at) "
                     "VALUES ('q-99990', ?, '[]', '项目深挖', '评测架构', 1, '2026-01-01', '2026-01-01')",
                     ("你做评测双写时为什么不直接用 Phoenix 的 run_experiment？",))
        conn.commit()
        conn.close()
        bq = retro.build_questions(rid)
        texts = [x["text"] for x in bq["inserted"]]
        check("重复题被跳过", all("为什么不直接用 Phoenix" not in t for t in texts))
        check("非法 qtype 被矫正为项目深挖",
              all(x["qtype"] in retro.VALID_QTYPES for x in bq["inserted"]))
        check("入池 2 题（1 重复 + 2 新）", len(bq["inserted"]) == 2, str(len(bq["inserted"])))
        qids = [x["question_id"] for x in bq["inserted"]]
        check("question_ids 记录到复盘", json.loads(
            get_conn().execute("SELECT question_ids_json FROM retrospectives WHERE id=?", (rid,)).fetchone()["question_ids_json"]) == qids)

        # 4. 建卡走审批
        print("[4] cards")
        # 测试可重复跑：先清掉之前测试留下的同标题 pending 草稿（draft_cards 有 pending 去重）
        conn = get_conn()
        conn.execute("DELETE FROM agent_actions WHERE kind='create_card' AND status='pending' "
                     "AND payload_json LIKE '%【复盘】评测双写架构%'")
        conn.commit()
        conn.close()
        bc = retro.build_cards(rid, limit=2)
        check("至少起草 1 张卡（另一题可能已被已有卡覆盖）", len(bc["drafted"]) >= 1, str(bc))
        rows = get_conn().execute(
            "SELECT kind, status FROM agent_actions WHERE id IN (%s)" % ",".join("?" * len(bc["drafted"])),
            [x["action_id"] for x in bc["drafted"]]).fetchall()
        check("产物是 create_card/pending（不直写卡）",
              len(rows) == len(bc["drafted"]) and all(r["kind"] == "create_card" and r["status"] == "pending" for r in rows),
              str([dict(r) for r in rows]))
        check("卡不在 kb_cards（审批前）", get_conn().execute(
            "SELECT COUNT(*) AS n FROM kb_cards WHERE title LIKE '%评测双写架构%'").fetchone()["n"] == 0)

        # 5. run_pipeline 全链路 + 失败落表
        print("[5] pipeline")
        # 假 LLM 输出固定，前几步已把题入池；清掉同文题，让 pipeline 有新题可入
        conn = get_conn()
        conn.execute("DELETE FROM question_pool WHERE text LIKE '%run_experiment%' OR text LIKE '%版本管理%' OR text LIKE '%qtype 是非法值%'")
        conn.execute("DELETE FROM agent_actions WHERE kind='create_card' AND status='pending'")
        conn.commit()
        conn.close()
        out = retro.run_pipeline(CONTENT, title="流水线复盘", build_cards_flag=True, card_limit=1)
        check("流水线全通", out.get("points") == 3 and out.get("questions") == 3
              and out.get("skipped_dup") == 0 and out.get("cards_drafted") == 1, str(out))
        check("流水线产物进审批队列", get_conn().execute(
            "SELECT COUNT(*) AS n FROM agent_actions WHERE status='pending' AND reason LIKE '%复盘文档提炼%'"
            ).fetchone()["n"] >= 1)

        def boom(*a, **k):
            raise RuntimeError("模拟 LLM 挂了")
        llm.chat_parsed = boom
        out2 = retro.run_pipeline(CONTENT, title="会失败的复盘")
        check("中途失败 error 落表且不抛异常",
              out2.get("error") and "模拟 LLM 挂了" in out2["error"], str(out2))
        row = get_conn().execute("SELECT status, error FROM retrospectives WHERE id=?",
                                 (out2["retro_id"],)).fetchone()
        check("失败状态可见", row["error"] is not None and "模拟" in (row["error"] or ""))

        # 6. 安全面
        print("[6] path safety")
        try:
            retro.ingest_from_doc("../../etc/passwd")
            check("越界路径被拒绝", False)
        except ValueError:
            check("越界路径被拒绝", True)
        try:
            retro.ingest_from_doc("docs/不存在的文档.md")
            check("不存在的文档被拒绝", False)
        except ValueError:
            check("不存在的文档被拒绝", True)
        docs = retro.list_docs()
        check("list_docs 能列出复盘文档", any("复盘-自进化闭环" in d["path"] for d in docs), str(docs[:3]))
    finally:
        llm.chat_parsed = orig

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
