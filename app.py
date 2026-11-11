"""
百度知道答题助手 —— Streamlit 可视化面板

启动方式:
    streamlit run app.py --server.runOnSave true

面板功能:
    左侧 Sidebar  → Cookie / 搜索词 / 节流参数 配置
    Tab 1 问题检索 → 搜索 → 分类 → 展示
    Tab 2 资源库   → 增删改查资源
    Tab 3 自动答题 → 匹配预览 → Dry Run → 开始答题（后台跑）
    Tab 4 统计    → 答题数 / 成功率 / 命中分布 / 趋势图
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import config
from classifier import QuestionClassifier
from resources import ResourceDB, Resource
from crawler import Question, collect_questions, fetch_question_detail
from answerer import Answerer, AnswerLog

# ---------------------------------------------------------------------------
# 全局状态初始化
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="百度知道答题助手",
    page_icon="🤖",
    layout="wide",
)


def _init_session():
    st.session_state.setdefault("settings", config.load())
    st.session_state.setdefault("questions", [])        # List[Question]
    st.session_state.setdefault("resource_qs", [])     # List[Question] 筛选后
    st.session_state.setdefault("matched_pairs", [])   # [(Question, Resource, score)]
    st.session_state.setdefault("answer_logs", [])     # List[AnswerLog]
    st.session_state.setdefault("log_msgs", [])       # 日志缓冲
    st.session_state.setdefault("is_collecting", False)
    st.session_state.setdefault("is_answering", False)


_init_session()

res_db = ResourceDB()
classifier = QuestionClassifier()

# ---------------------------------------------------------------------------
# Sidebar —— 配置面板
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚙️ 配置中心")
    st.caption("所有设置会自动保存到 data/settings.json")

    settings = st.session_state["settings"]

    # 登录状态指示器 + 手动登录按钮
    st.markdown("#### 🔐 登录状态")
    from pathlib import Path as _P
    _login_state = _P("data/login_state.json")
    if _login_state.exists():
        st.success(f"✅ 已保存登录会话")
        st.caption(f"文件: {_login_state.name}")
        if st.button("🔄 重新登录", use_container_width=True):
            import subprocess
            subprocess.Popen(
                [".\\venv\\Scripts\\python.exe", "login_helper.py"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            st.info("已在新窗口打开浏览器，请按提示登录")
    else:
        st.warning("⚠️ 未登录，答题会失败")
        if st.button("🔑 手动登录（推荐）", type="primary", use_container_width=True):
            import subprocess
            subprocess.Popen(
                [".\\venv\\Scripts\\python.exe", "login_helper.py"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            st.info("已在新窗口打开浏览器，登录完成后回到终端按回车保存")

    # Cookie（备选方案）
    with st.expander("🔑 高级：手动填 Cookie（备选）", expanded=False):
        st.caption("仅当上方「手动登录」无法使用时才需要填这个")
        cookie = st.text_area(
            "百度 Cookie 字符串",
            value=settings.get("baidu_cookie", ""),
            height=100,
            help="浏览器登录 zhidao.baidu.com 后，F12 → Network → 复制 Cookie 请求头整段字符串粘贴",
        )
        settings["baidu_cookie"] = cookie
        config.save(settings)

    # 搜索词
    st.markdown("#### 🔍 搜索关键词")
    queries_input = st.text_area(
        "每行一个",
        value="\n".join(settings.get("search_queries", [])),
        height=100,
    )
    queries = [q.strip() for q in queries_input.split("\n") if q.strip()]
    settings["search_queries"] = queries
    config.save(settings)

    # 节流参数 —— 注意 streamlit 1.67 要求 value/step/min/max 类型一致
    st.markdown("#### 🕐 节流参数")
    settings["max_questions_per_query"] = st.number_input(
        "每个搜索词最多采集",
        min_value=5, max_value=100, step=5,
        value=int(settings.get("max_questions_per_query", 30)),
    )
    settings["request_interval"] = st.number_input(
        "检索请求间隔（秒）",
        min_value=1.0, max_value=30.0, step=1.0,
        value=float(settings.get("request_interval", 3.0)),
    )
    settings["answer_interval"] = st.number_input(
        "答题间隔（秒）",
        min_value=5.0, max_value=120.0, step=5.0,
        value=float(settings.get("answer_interval", 15.0)),
    )
    settings["max_answers_per_question"] = st.number_input(
        "单条问题最多答几次",
        min_value=1, max_value=10, step=1,
        value=int(settings.get("max_answers_per_question", 3)),
    )
    settings["playwright_headless"] = st.checkbox(
        "答题时无头模式",
        value=settings.get("playwright_headless", True),
        help="关掉就能看到浏览器在点什么，调试用",
    )
    config.save(settings)

    # 状态
    st.divider()
    st.markdown("#### 📊 当前状态")
    st.write(f"资源库: **{len(res_db.resources)}** 条")
    st.write(f"已答问题计数: **{len(Answerer._load_answered())}** 条")

# ---------------------------------------------------------------------------
# 日志辅助
# ---------------------------------------------------------------------------

def log(msg: str):
    """往 session_state 里追加一条日志"""
    st.session_state["log_msgs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    # 保留最近 500 条
    if len(st.session_state["log_msgs"]) > 500:
        st.session_state["log_msgs"] = st.session_state["log_msgs"][-500:]


# ---------------------------------------------------------------------------
# Tab 1 —— 问题检索
# ---------------------------------------------------------------------------

tab1, tab2, tab3, tab4 = st.tabs([
    "🔍 问题检索", "📚 资源库", "🚀 自动答题", "📊 统计仪表盘"
])

# ============================================================
# Tab 1
# ============================================================
with tab1:
    st.header("🔍 问题检索")
    st.caption("用左侧配置的搜索词在百度知道上采集问题，自动识别是否为「求资源」类")

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("🚀 开始采集", disabled=st.session_state["is_collecting"],
                     type="primary", use_container_width=True):
            st.session_state["is_collecting"] = True
            log("开始采集问题...")

            def _run():
                try:
                    qs = asyncio.run(collect_questions(
                        queries=settings["search_queries"],
                        max_per_query=settings["max_questions_per_query"],
                        interval=settings["request_interval"],
                    ))
                    log(f"第一轮采集到 {len(qs)} 条，开始补详情...")

                    # 并发生详情（限流 3）
                    async def enrich_all():
                        sem = asyncio.Semaphore(3)
                        async with httpx_async_client() as client:
                            async def work(q):
                                async with sem:
                                    return await fetch_question_detail(q, client, 1.5)
                            tasks = [work(q) for q in qs]
                            return await asyncio.gather(*tasks)

                    import httpx
                    enriched = asyncio.run(enrich_all())

                    # 分类
                    resource_qs = []
                    for q in enriched:
                        q.is_resource = classifier.is_resource_request(q.title, q.snippet)
                        q.matched_keywords = classifier.matched_keywords(q.title, q.snippet)
                        if q.is_resource:
                            resource_qs.append(q)

                    st.session_state["questions"] = list(enriched)
                    st.session_state["resource_qs"] = resource_qs
                    log(f"完成！共收集 {len(enriched)} 条，其中资源需求 {len(resource_qs)} 条")
                except Exception as e:
                    log(f"[error] 采集失败: {e}")
                finally:
                    st.session_state["is_collecting"] = False

            threading.Thread(target=_run, daemon=True).start()

        if st.button("🗑️ 清空结果", use_container_width=True):
            st.session_state["questions"] = []
            st.session_state["resource_qs"] = []
            log("已清空")

    with col2:
        st.write(f"已采集 **{len(st.session_state['questions'])}** 条，"
                 f"其中资源需求 **{len(st.session_state['resource_qs'])}** 条")
        if st.session_state["is_collecting"]:
            st.info("⏳ 正在采集中...请稍候")

    # 表格展示
    if st.session_state["questions"]:
        df = pd.DataFrame([q.to_dict() for q in st.session_state["questions"]])
        # 加一个 tag 列方便看
        df["分类"] = df["is_resource"].map({True: "🔴 资源需求", False: "⚪ 其他"})
        show_df = df[["分类", "title", "source_query", "matched_keywords", "url"]].rename(
            columns={"title": "问题标题", "source_query": "搜索词", "matched_keywords": "命中关键词", "url": "链接"}
        )
        st.dataframe(show_df, use_container_width=True, hide_index=True,
                     column_config={"链接": st.column_config.LinkColumn()})
    else:
        st.info("还没采集到数据，点左侧按钮开始吧~")

    # 日志
    with st.expander("📋 运行日志", expanded=False):
        for line in reversed(st.session_state["log_msgs"][-100:]):
            st.code(line)


def httpx_async_client():
    """返回一个 httpx.AsyncClient 上下文管理器"""
    import httpx
    return httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-CN,zh;q=0.9"},
        follow_redirects=True, timeout=15,
    )

# ============================================================
# Tab 2 —— 资源库管理
# ============================================================
with tab2:
    st.header("📚 资源库管理")
    st.caption("所有资源保存在 data/resources.json，答题时按问题关键词自动匹配")

    # 表格
    rows = res_db.as_dicts()
    if rows:
        df = pd.DataFrame(rows)
        df["keywords"] = df["keywords"].apply(lambda x: ", ".join(x) if x else "")
        col_order = ["category", "keywords", "url", "password", "note"]
        df = df[[c for c in col_order if c in df.columns]]
        df = df.rename(columns={
            "category": "类别", "keywords": "关键词",
            "url": "链接", "password": "提取码", "note": "备注"
        })
        st.dataframe(df, use_container_width=True, hide_index=True,
                     column_config={"链接": st.column_config.LinkColumn()})
    else:
        st.warning("资源库还是空的，先在下面添加一条吧~")

    st.divider()

    # 新增表单
    st.subheader("➕ 添加资源")
    with st.form("add_resource", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            new_cat = st.text_input("类别", placeholder="如：电子书-科幻")
            new_url = st.text_input("资源链接", placeholder="https://pan.baidu.com/s/xxxxx")
            new_pwd = st.text_input("提取码", placeholder="可选")
        with c2:
            new_kw = st.text_area("关键词（逗号/空格分隔）", placeholder="三体, 刘慈欣, 科幻小说")
            new_note = st.text_area("备注（作为答案开头文案）", placeholder="《三体》全集，记得及时保存~")

        if st.form_submit_button("✅ 添加到资源库"):
            keywords = [k.strip() for k in new_kw.replace("，", ",").replace(" ", ",").split(",") if k.strip()]
            if not new_cat or not new_url or not keywords:
                st.error("类别、链接、关键词不能为空")
            else:
                res_db.add(Resource(
                    category=new_cat, keywords=keywords,
                    url=new_url, password=new_pwd, note=new_note,
                ))
                st.success(f"已添加：{new_cat}")
                st.rerun()

    # 删除（简单 list + 选择框）
    if rows:
        st.divider()
        st.subheader("🗑️ 删除资源")
        options = [f"[{i}] {r['category']} — {', '.join(r['keywords'][:3])}"
                   for i, r in enumerate(rows)]
        sel = st.selectbox("选择要删除的资源", options, key="del_sel")
        idx = int(sel.split("]")[0][1:])
        if st.button("🗑️ 确认删除", key="del_btn"):
            res_db.delete(idx)
            st.success("已删除")
            st.rerun()

# ============================================================
# Tab 3 —— 自动答题
# ============================================================
with tab3:
    st.header("🚀 自动答题")
    st.caption("对已采集的资源需求问题，用资源库匹配生成答案，再用 Playwright 模拟浏览器提交")

    # 第一步：匹配
    st.subheader("① 资源匹配预览")
    if not st.session_state.get("resource_qs"):
        st.warning("⚠️ 还没有可答的问题，先去「问题检索」Tab 跑一次采集吧")
    else:
        pairs = []
        for q in st.session_state["resource_qs"]:
            matches = res_db.match(f"{q.title} {q.snippet}", top_k=1)
            if matches:
                res, score = matches[0]
                pairs.append((q, res, score))
        st.session_state["matched_pairs"] = pairs

        st.write(f"共 **{len(st.session_state['resource_qs'])}** 条资源需求问题，"
                 f"其中 **{len(pairs)}** 条匹配到资源")

        if pairs:
            preview_rows = []
            for q, res, score in pairs:
                preview_rows.append({
                    "问题": q.title[:60],
                    "匹配资源": res.category,
                    "命中关键词": ", ".join(q.matched_keywords[:5]),
                    "链接": q.url,
                })
            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True,
                         hide_index=True,
                         column_config={"链接": st.column_config.LinkColumn()})

    st.divider()

    # Dry Run 预览答案
    st.subheader("② Dry Run —— 预览答案文本")
    pairs = st.session_state.get("matched_pairs", [])
    if pairs:
        preview_idx = st.selectbox(
            "选择一条看答案",
            range(len(pairs)),
            format_func=lambda i: f"[{pairs[i][2]}分] {pairs[i][0].title[:50]}",
        )
        q, res, score = pairs[preview_idx]
        answer_text = res.build_answer_text(q.title)
        st.code(answer_text, language="text")

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🎯 答题预览 OK，准备开始", type="secondary", disabled=True):
                pass
        with col_b:
            pass

    st.divider()

    # 开始答题
    st.subheader("③ 开始答题")
    col1, col2 = st.columns([1, 3])

    with col1:
        has_login = _P("data/login_state.json").exists() or bool(settings.get("baidu_cookie"))
        can_answer = has_login and bool(pairs)
        if not has_login:
            st.warning("⚠️ 先在左侧完成登录（点「手动登录」按钮）")

        if st.button("🚀 开始自动答题", disabled=not can_answer or st.session_state["is_answering"],
                     type="primary", use_container_width=True):
            st.session_state["is_answering"] = True
            st.session_state["answer_logs"] = []

            def _run():
                ans = Answerer(
                    cookie=settings["baidu_cookie"],
                    browser_path=settings.get("playwright_browser_path", "playwright-browsers"),
                    headless=settings["playwright_headless"],
                    max_per_question=settings["max_answers_per_question"],
                    log_callback=log,
                )
                try:
                    ans.start()
                    for q, res, score in st.session_state["matched_pairs"]:
                        text = res.build_answer_text(q.title)
                        log(f"准备回答: {q.title[:50]}")
                        log_obj = ans.answer(q.url, q.qid, text)
                        st.session_state["answer_logs"].append(log_obj)
                        time.sleep(settings["answer_interval"] + 2)
                except Exception as e:
                    log(f"[error] 答题异常: {e}")
                finally:
                    ans.stop()
                    st.session_state["is_answering"] = False
                    log("答题流程结束")

            threading.Thread(target=_run, daemon=True).start()

        if st.button("⏹️ 停止", disabled=not st.session_state["is_answering"], use_container_width=True):
            # 注：线程里的 playwright 没法中途优雅停，这个按钮只做软提示
            st.warning("当前这条答完会停（线程安全考虑，建议等它跑完）")

    with col2:
        if st.session_state["is_answering"]:
            st.info("⏳ 答题进行中...实时日志在下方")
        # 实时日志（每次刷新面板都会重新渲染，所以自动答题时需要定时触发 rerun）
        if st.session_state["log_msgs"]:
            st.text_area("📋 实时日志",
                         "\n".join(reversed(st.session_state["log_msgs"][-80:])),
                         height=320, disabled=True)

    # 答题结果表
    if st.session_state["answer_logs"]:
        st.divider()
        st.subheader("📝 本次答题结果")
        logs = st.session_state["answer_logs"]
        df_logs = pd.DataFrame([l.to_row() for l in logs])
        df_logs["状态"] = df_logs["success"].map({True: "✅ 成功", False: "❌ 失败/跳过"})
        show_cols = ["状态", "time", "qid", "preview", "url"]
        show_cols = [c for c in show_cols if c in df_logs.columns]
        st.dataframe(df_logs[show_cols], use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn()})

# ============================================================
# Tab 4 —— 统计仪表盘
# ============================================================
with tab4:
    st.header("📊 统计仪表盘")

    def _load_history() -> dict:
        path = Path("data/answered.json")
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    ans_map = _load_history()
    total_answered = sum(ans_map.values())
    unique_qids = len(ans_map)

    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("累计答题次数", total_answered)
    kpi2.metric("覆盖问题数", unique_qids)
    kpi3.metric("资源库数量", len(res_db.resources))

    st.divider()

    # 资源命中分布（用已采集的问题 + 匹配结果）
    st.subheader("🎯 资源匹配分布（本轮采集）")
    if st.session_state.get("matched_pairs"):
        cat_counts = {}
        for _, res, _ in st.session_state["matched_pairs"]:
            cat_counts[res.category] = cat_counts.get(res.category, 0) + 1
        df_dist = pd.DataFrame(
            [{"类别": k, "命中次数": v} for k, v in cat_counts.items()]
        ).sort_values("命中次数", ascending=False)
        st.bar_chart(df_dist.set_index("类别")["命中次数"])
    else:
        st.info("先在「问题检索」里跑一次，再来这里看分布~")

    st.divider()

    # 最近答题日志
    st.subheader("📝 最近答题记录")
    answered_path = Path("data/answered.json")
    if answered_path.exists():
        st.code(answered_path.read_text(encoding="utf-8"), language="json")
    else:
        st.info("还没有答题记录")
