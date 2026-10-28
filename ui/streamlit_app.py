"""Streamlit 前端：采集台 / 知识库 / 面经库 / 模拟面试 / 复习·错题 / 雷达·冲刺。
运行：streamlit run ui/streamlit_app.py
后端地址可用环境变量 JOBPREP_API 覆盖（部署到服务器时用）。
"""
import json
import os

# 前端 → 后端是本机回环请求，绝不能走系统代理：代理连不回 localhost 时
# 健康检查失败，所有按钮会被 disabled（实测踩坑：全局代理劫持 localhost）。
_no_proxy = {p.strip() for p in os.environ.get("NO_PROXY", "").split(",") if p.strip()}
_no_proxy |= {"localhost", "127.0.0.1"}
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(sorted(_no_proxy))

import httpx
import streamlit as st

API = os.getenv("JOBPREP_API", "http://localhost:8765")

st.set_page_config(page_title="求职准备知识库", page_icon="📚", layout="wide")

try:
    health = httpx.get(f"{API}/health", timeout=3).json()
    st.sidebar.success("后端已连接")
except Exception:
    st.sidebar.error("后端未启动：uvicorn app.main:app --port 8765")
    health = None

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📥 采集台", "📚 知识库", "🗂 面经库", "🎤 模拟面试", "🔁 复习·错题", "📈 雷达·冲刺"])

# ============ 采集台 ============
with tab1:
    st.subheader("投喂面经")
    c1, c2 = st.columns([3, 2])
    source_type = c1.selectbox("来源", ["paste", "url", "bookmarklet"], index=0)
    url = c2.text_input("来源链接（可选/bookmarklet 自动带入）", value=st.query_params.get("url", ""))
    text = st.text_area("面经正文（粘贴；或填 URL 由后端自动抓取）", height=200,
                        value=st.query_params.get("sel", ""))

    if st.button("投喂", type="primary", disabled=not health):
        try:
            r = httpx.post(f"{API}/api/ingests", timeout=30, json={
                "source_type": source_type, "text": text, "url": url,
                "selection": st.query_params.get("sel", ""),
            })
            if r.status_code == 200:
                st.success(f"已进入采集队列：{r.json()['id']}")
                st.rerun()
            else:
                st.error(r.json().get("detail", r.text))
        except Exception as e:
            st.error(f"请求失败：{e}")

    st.divider()
    st.subheader("采集队列")
    status_filter = st.selectbox("状态筛选", ["pending", "extracted", "confirmed", "discarded", "failed", "全部"], index=0)
    if health:
        ingests = httpx.get(f"{API}/api/ingests", params={"status": status_filter if status_filter != "全部" else None}, timeout=10).json()
        for ing in ingests:
            with st.expander(f"{ing['id']}  [{ing['status']}]  {ing.get('source_url') or ''}"):
                st.caption(f"来源：{ing['source_type']}  原文：{ing['raw_path']}")
                if ing["status"] == "pending":
                    if st.button("LLM 抽取", key=f"ex-{ing['id']}"):
                        r = httpx.post(f"{API}/api/ingests/{ing['id']}/extract", timeout=120)
                        st.toast("抽取完成" if r.status_code == 200 else f"失败：{r.text}")
                        st.rerun()
                if ing["status"] == "extracted":
                    ext = ing.get("extracted", {})
                    with st.form(f"cf-{ing['id']}"):
                        cc1, cc2, cc3 = st.columns(3)
                        company = cc1.text_input("公司", value=ext.get("company") or "")
                        position = cc2.text_input("岗位", value=ext.get("position") or "")
                        direction = cc3.selectbox("方向", ["backend", "agent", "bigdata"],
                                                  index=["backend", "agent", "bigdata"].index(ext.get("direction") or "bigdata"))
                        questions = ext.get("questions", [])
                        st.write(f"抽取到 {len(questions)} 道题（取消勾选即丢弃）：")
                        kept = []
                        for qq in questions:
                            if st.checkbox(qq.get("text", ""), value=True, key=f"q-{ing['id']}-{qq.get('order')}"):
                                kept.append({"keep": True, "text": qq.get("text", ""),
                                             "qtype": qq.get("qtype", ""), "topic": qq.get("topic_hint", "")})
                        if st.form_submit_button("确认入库", type="primary"):
                            r = httpx.post(f"{API}/api/ingests/{ing['id']}/confirm", timeout=30, json={
                                "company": company, "position": position, "direction": direction,
                                "questions": kept})
                            if r.status_code == 200:
                                st.success(f"入库成功：{r.json()['interview_id']}，保留 {r.json()['questions_kept']} 题")
                                st.rerun()
                            else:
                                st.error(r.text)
                if ing["status"] in ("pending", "extracted", "failed"):
                    if st.button("丢弃", key=f"di-{ing['id']}"):
                        httpx.post(f"{API}/api/ingests/{ing['id']}/discard", timeout=10)
                        st.rerun()

# ============ 知识库 ============
with tab2:
    st.subheader("快速录入知识卡片")
    with st.form("add-card"):
        cc1, cc2, cc3 = st.columns(3)
        title = cc1.text_input("标题*")
        domain = cc2.selectbox("领域", ["backend", "agent", "bigdata", "cs"])
        topic = cc3.text_input("主题（如 Redis / Spark / RAG）", value="通用")
        question = st.text_input("面试官会怎么问（问题化表述）")
        points = st.text_area("要点（每行一条）", height=100)
        submitted = st.form_submit_button("创建卡片", disabled=not health)
        if submitted and title:
            r = httpx.post(f"{API}/api/cards", timeout=15, json={
                "title": title, "domain": domain, "topic": topic, "question": question,
                "points": [x for x in points.splitlines() if x.strip()],
            })
            if r.status_code == 200:
                st.success(f"已创建 {r.json()['id']}，md 文件与索引同步完成")
            else:
                st.error(r.text)

    st.divider()
    st.subheader("检索")
    qq = st.text_input("关键词（FTS 全文检索）")
    if health and st.button("搜索"):
        rows = httpx.get(f"{API}/api/cards", params={"q": qq} if qq else {}, timeout=10).json()
        for c in rows:
            with st.expander(f"{c['id']}  {c['title']}  [mastery {c['mastery']}]"):
                st.caption(f"{c['domain']} / {c['topic']}  文件：{c['file_path']}")
                st.write("**Q：**" + (c.get("question") or ""))
                st.write("\n".join(f"- {p}" for p in c.get("points", [])))

# ============ 面经库 ============
with tab3:
    st.subheader("统计")
    if health:
        s = httpx.get(f"{API}/api/stats", timeout=10).json()
        m1, m2, m3 = st.columns(3)
        m1.metric("知识卡片", sum(s["cards_by_domain"].values()))
        m2.metric("面经", sum(s["interviews_by_direction"].values()))
        m3.metric("待处理采集", s["pending_ingests"])
        st.caption(f"卡片分布：{json.dumps(s['cards_by_domain'], ensure_ascii=False)}  |  "
                   f"面经分布：{json.dumps(s['interviews_by_direction'], ensure_ascii=False)}")
        st.subheader("高频题 Top10")
        for q in s["top_questions"]:
            st.write(f"`{q['freq_count']}次` **{q['text']}**  \n`{q['qtype']}` `{q['topic']}`")
        st.subheader("题目池检索")
        qkw = st.text_input("关键词", key="qpool")
        if st.button("查题目池"):
            rows = httpx.get(f"{API}/api/questions", params={"q": qkw} if qkw else {}, timeout=10).json()
            for q in rows:
                st.write(f"`{q['id']}`（{q['freq_count']}次）{q['text']}  `{q['qtype']}`")

# ============ 模拟面试（阶段 3） ============
with tab4:
    st.subheader("模拟面试")

    if "mock_sid" not in st.session_state:
        st.session_state.mock_sid = None
    if "mock_idx" not in st.session_state:
        st.session_state.mock_idx = 0
    if "mock_last_ev" not in st.session_state:
        st.session_state.mock_last_ev = None

    if not health:
        st.stop()

    # --- 开场：配置并组卷 ---
    if not st.session_state.mock_sid:
        with st.form("start-mock"):
            cc1, cc2 = st.columns(2)
            direction = cc1.selectbox("面试方向", ["bigdata", "backend", "agent"])
            count = cc2.number_input("题目数量", 3, 15, value=6)
            persona = st.text_input("面试官 persona（可选，如：某大厂数据平台负责人）")
            jd = st.text_area("目标 JD（可选，组卷加权用）", height=80)
            if st.form_submit_button("开始面试", type="primary"):
                r = httpx.post(f"{API}/api/mock/sessions", timeout=180, json={
                    "direction": direction, "persona": persona, "jd_text": jd, "question_count": count})
                if r.status_code == 200:
                    st.session_state.mock_sid = r.json()["session_id"]
                    st.session_state.mock_idx = 0
                    st.session_state.mock_last_ev = None
                    st.rerun()
                else:
                    st.error(r.text)

    # --- 进行中：逐题问答 ---
    if st.session_state.mock_sid:
        s = httpx.get(f"{API}/api/mock/sessions/{st.session_state.mock_sid}", timeout=15).json()
        qs = s["questions"]
        answered = [a for a in s["answers"] if a]
        st.progress(len(answered) / len(qs), text=f"进度 {len(answered)}/{len(qs)}　persona：{s['persona'] or '默认'}")

        # 展示已答题目与评分
        for i, a in enumerate(s["answers"]):
            if a and i < st.session_state.mock_idx:
                with st.expander(f"✅ 第{i+1}题 {qs[i]['text']}（均分 {round(sum(a['scores'].values())/4,1)}）"):
                    st.write("**点评：**" + (a["feedback"] or ""))
                    if a.get("followups") and a["followups"] != "null":
                        st.info("追问：" + str(a["followups"]))

        # 当前题
        if st.session_state.mock_idx < len(qs):
            i = st.session_state.mock_idx
            q = qs[i]
            st.markdown(f"### 第 {i+1} 题　`{q['qtype']}` `{q['topic']}`")
            st.write(q["text"])
            ans = st.text_area("你的回答", height=160, key=f"ans-{i}")
            ca, cb, cc = st.columns(3)
            if ca.button("提交本题", type="primary"):
                r = httpx.post(f"{API}/api/mock/sessions/{st.session_state.mock_sid}/answer",
                               timeout=120, json={"index": i, "answer_text": ans})
                if r.status_code == 200:
                    st.session_state.mock_last_ev = r.json()["evaluation"]
                    st.rerun()
                else:
                    st.error(r.text)
            if cc.button("跳过此题"):
                r = httpx.post(f"{API}/api/mock/sessions/{st.session_state.mock_sid}/answer",
                               timeout=120, json={"index": i, "answer_text": ""})
                if r.status_code == 200:
                    st.session_state.mock_last_ev = r.json()["evaluation"]
                    st.rerun()

            # 展示本题评分，按钮进入下一题
            if st.session_state.mock_last_ev:
                ev = st.session_state.mock_last_ev
                sc = ev["scores"]
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("正确性", sc["correctness"]); m2.metric("深度", sc["depth"])
                m3.metric("结构表达", sc["structure"]); m4.metric("亮点", sc["highlight"])
                st.write("**点评：**" + ev.get("feedback", ""))
                if ev.get("followup") and ev["followup"] != "null":
                    st.info("追问（可自己想一想）：**" + str(ev["followup"]) + "**")
                if cb.button("下一题 →"):
                    st.session_state.mock_idx += 1
                    st.session_state.mock_last_ev = None
                    st.rerun()
        else:
            # 全部答完 -> 生成总评
            st.success("所有题目已完成，点击生成总评报告")
            if st.button("生成本次面试总评", type="primary"):
                with st.spinner("面试官正在写评语..."):
                    r = httpx.post(f"{API}/api/mock/sessions/{st.session_state.mock_sid}/finish", timeout=180)
                if r.status_code == 200:
                    o = r.json()
                    st.session_state.mock_report = o
                    st.session_state.mock_sid = None
                    st.session_state.mock_idx = 0
                    st.session_state.mock_last_ev = None
                    st.rerun()
                else:
                    st.error(r.text)

    # --- 报告展示（独立于进行中状态） ---
    if st.session_state.get("mock_report"):
        o = st.session_state.mock_report
        st.divider()
        st.subheader("本次面试报告")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("正确性", o["avg"]["correctness"]); m2.metric("深度", o["avg"]["depth"])
        m3.metric("结构表达", o["avg"]["structure"]); m4.metric("亮点", o["avg"]["highlight"])
        st.metric("总均分", o["overall_avg"])
        st.write("**总评：**" + o["report"].get("summary", ""))
        st.write("**通过评估：**" + o["report"].get("pass_estimate", ""))
        ca, cb = st.columns(2)
        ca.markdown("**亮点**\n" + "\n".join(f"- {x}" for x in o["report"].get("strengths", [])))
        cb.markdown("**短板**\n" + "\n".join(f"- {x}" for x in o["report"].get("weaknesses", [])))
        st.markdown("**改进建议**\n" + "\n".join(f"- {x}" for x in o["report"].get("advice", [])))
        st.caption(f"完整报告已归档：{o['report_path']}（阶段 4 将把弱项自动回写错题本与掌握度）")
        if st.button("清除报告，再来一场"):
            st.session_state.mock_report = None
            st.rerun()

# ============ 复习·错题（阶段 4 数据闭环） ============
with tab5:
    if not health:
        st.stop()
    st.subheader("每日简报")
    try:
        b = httpx.get(f"{API}/api/daily", timeout=15).json()
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("今日到期复习", b["due_cards"])
        b2.metric("未解决错题", b["open_mistakes"])
        b3.metric("待处理采集", b["pending_ingests"])
        b4.metric("近 3 场均分", "/".join(str(x) for x in b["recent_mock_avgs"]) or "—")
        wc = b["weak"].get("weak_cards", [])
        if wc:
            st.caption("薄弱卡片：" + "、".join(f"{c['title']}({c['mastery']})" for c in wc[:5]))
    except Exception as e:
        st.error(f"简报获取失败：{e}")

    st.divider()
    st.subheader("今日到期复习（快问快答）")
    try:
        due = httpx.get(f"{API}/api/srs/due", timeout=15).json()
    except Exception:
        due = []
    if not due:
        st.info("今天没有到期卡片，去看看面经采集台攒题吧")
    for c in due:
        with st.expander(f"`{c['mastery']}` {c['title']}（{c['domain']}/{c['topic']}）"):
            st.write("**Q：**" + (c["question"] or "（无问题化表述，直接回忆要点）"))
            r1, r2 = st.columns(2)
            if r1.button("✅ 答对了", key=f"p-{c['id']}"):
                r = httpx.post(f"{API}/api/srs/review", timeout=15, json={"card_id": c["id"], "result": "pass"})
                if r.status_code == 200:
                    st.toast(f"掌握度 {r.json()['mastery']}，{r.json()['interval_days']} 天后再来")
                    st.rerun()
            if r2.button("❌ 没答上", key=f"f-{c['id']}"):
                r = httpx.post(f"{API}/api/srs/review", timeout=15, json={"card_id": c["id"], "result": "fail"})
                if r.status_code == 200:
                    st.toast("明天再来一次")
                    st.rerun()

    st.divider()
    st.subheader("错题本（模拟面试自动生成）")
    try:
        ms = httpx.get(f"{API}/api/mistakes", params={"only_open": True}, timeout=15).json()
    except Exception:
        ms = []
    if not ms:
        st.success("没有未解决的错题，保持住")
    for m in ms:
        with st.expander(f"[{m.get('cause_type')}] {m['raw_text'][:50]}"):
            st.write("**点评：**" + (m.get("feedback") or ""))
            st.caption(f"来源会话：{m.get('mock_session_id')}　主题：{m.get('topic')}")
            if st.button("标记已解决", key=f"rm-{m['id']}"):
                httpx.post(f"{API}/api/mistakes/{m['id']}/resolve", timeout=15)
                st.rerun()

# ============ 雷达·冲刺 + 学习教练 Agent（阶段 5） ============
with tab6:
    if not health:
        st.stop()
    st.subheader("能力雷达")
    try:
        rd = httpx.get(f"{API}/api/radar", timeout=15).json()
        d1, d2 = st.columns(2)
        for i, (dom, info) in enumerate(rd["by_domain"].items()):
            (d1 if i % 2 == 0 else d2).metric(f"{dom}", info["avg"], f"{info['cards']} 张卡")
        dom_sel = st.selectbox("查看主题明细", list(rd["topics"].keys()) or ["（暂无数据）"])
        if dom_sel in rd["topics"]:
            chart = {t["topic"]: t["mastery"] for t in rd["topics"][dom_sel]}
            st.bar_chart(chart)
        if rd["mock_trend"]:
            st.line_chart({t["date"]: t["avg"] for t in rd["mock_trend"]})
        else:
            st.caption("打完一场模拟面试后，这里会出现均分趋势线")
    except Exception as e:
        st.error(f"雷达获取失败：{e}")

    st.divider()
    st.subheader("公司定向冲刺")
    with st.form("sprint"):
        s1, s2, s3 = st.columns(3)
        sp_company = s1.text_input("目标公司（匹配面经库）")
        sp_dir = s2.selectbox("方向", ["bigdata", "backend", "agent"], key="sprint-dir")
        sp_n = s3.number_input("题数", 3, 15, 6, key="sprint-n")
        if st.form_submit_button("组冲刺卷"):
            r = httpx.post(f"{API}/api/mock/sessions", timeout=180, json={
                "direction": sp_dir, "question_count": sp_n, "company": sp_company})
            if r.status_code == 200:
                st.session_state.mock_sid = r.json()["session_id"]
                st.session_state.mock_idx = 0
                st.session_state.mock_last_ev = None
                st.success(f"冲刺卷已组好（{sp_company or '全部'} 公司题优先），切到 🎤 模拟面试 页签开始作答")
            else:
                st.error(r.text)

    st.divider()
    st.subheader("学习教练 Agent")
    st.caption("Agent 自主调用工具查询你的真实数据（简报/错题/到期卡/高频题），生成今日学习计划，全程轨迹可查")
    if st.button("生成今日学习计划", type="primary"):
        with st.spinner("Agent 正在调用工具分析数据..."):
            r = httpx.post(f"{API}/api/agent/plan", timeout=180, json={"direction": "bigdata"})
        if r.status_code == 200:
            j = r.json()
            if j["status"] == "done":
                p = j["plan"]
                st.write("**总结：**" + p.get("summary", ""))
                if p.get("key_findings"):
                    with st.expander("Agent 读出的关键结论（均带数据依据）", expanded=True):
                        for f in p["key_findings"]:
                            st.markdown(f"- {f}")
                st.caption(f"计划 {len(p.get('plan', []))} 项 / 合计 {p.get('total_minutes', 0)} 分钟 · prompt v{p.get('prompt_version')}")
                for it in p.get("plan", []):
                    st.markdown(f"**P{it.get('priority')}. {it.get('action')}**（{it.get('minutes')} 分钟）\n\n> 依据：{it.get('reason') or '—'}")
                with st.expander(f"Agent 轨迹（run_id: {j['run_id']}，已落库可回放）"):
                    for s in j["trace"]:
                        st.write(f"· iter{s.get('iter')} `{s.get('tool')}` → {str(s.get('result_preview') or s.get('summary'))[:120]}")
            else:
                st.error(f"Agent 执行失败：{j.get('error')}")
        else:
            st.error(r.text)

# ============ 追加到 tab5：Agent 补救建议（Human-in-the-loop，阶段 6） ============
with tab5:
    st.divider()
    st.subheader("Agent 补救建议（写操作需你审批）")
    st.caption("教练 Agent 基于最近一场面试报告起草补救动作；批准后才会真正改写知识库/复习计划")
    ca, cb = st.columns([1, 1])
    if ca.button("从最近一场面试起草建议"):
        try:
            sessions = httpx.get(f"{API}/api/mock/sessions", timeout=15).json()
            finished = [s for s in sessions if s.get("finished_at")]
            if not finished:
                st.warning("还没有已结束的面试，先打一场再来")
            else:
                with st.spinner("教练 Agent 正在分析报告..."):
                    r = httpx.post(f"{API}/api/agent/remediation/{finished[0]['id']}/draft", timeout=180)
                if r.status_code == 200:
                    st.toast(f"已生成 {len(r.json()['actions'])} 条待审批建议")
                    st.rerun()
                else:
                    st.error(r.text)
        except Exception as e:
            st.error(f"失败：{e}")
    if cb.button("刷新"):
        st.rerun()

    try:
        pending = httpx.get(f"{API}/api/agent/actions", params={"status": "pending"}, timeout=15).json()
    except Exception:
        pending = []
    if not pending:
        st.info("暂无待审批建议")
    for a in pending:
        p = a["payload"]
        if a["kind"] == "merge_card":
            head = f"{p.get('duplicate_card_id', '?')} → {p.get('keeper_card_id', '?')}"
        else:
            head = p.get("title") or p.get("card_id") or ""
        with st.expander(f"[{a['kind']}] {head}  —  {a.get('reason', '')[:40]}"):
            st.json(p)
            c1, c2 = st.columns(2)
            if c1.button("✅ 批准执行", key=f"ap-{a['id']}"):
                r = httpx.post(f"{API}/api/agent/actions/{a['id']}/approve", timeout=30)
                st.toast("已执行" if r.status_code == 200 else f"失败：{r.text}")
                st.rerun()
            if c2.button("❌ 驳回", key=f"rj-{a['id']}"):
                httpx.post(f"{API}/api/agent/actions/{a['id']}/reject", timeout=15)
                st.rerun()
