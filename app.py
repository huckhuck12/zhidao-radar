"""
百度知道答题助手 —— Streamlit 可视化面板

启动方式:
    streamlit run app.py --server.runOnSave true

面板功能:
    左侧 Sidebar  → 运行模式（只读 / 答题）/ Cookie / 搜索词 / 节流参数
    Tab 1 问题检索 → 搜索 → 分类 → 展示 → 导出
    Tab 2 资源库   → 增删改查资源
    Tab 3 自动答题 → 匹配预览 → Dry Run → 开始答题（仅答题模式）
    Tab 4 统计    → 答题数 / 命中分布
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import config
from classifier import QuestionClassifier
from resources import ResourceDB, Resource
from crawler import Question, SOURCES
from answerer import Answerer, AnswerLog

# ---------------------------------------------------------------------------
# 全局状态初始化
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="zhidao-radar",
    page_icon="🎯",
    layout="wide",
)


def _init_session():
    st.session_state.setdefault("settings", config.load())
    st.session_state.setdefault("questions", [])        # List[Question]
    st.session_state.setdefault("resource_qs", [])     # List[Question] 筛选后
    st.session_state.setdefault("matched_pairs", [])   # [(Question, Resource, score)]
    st.session_state.setdefault("answer_logs", [])     # List[AnswerLog]
    st.session_state.setdefault("is_collecting", False)
    st.session_state.setdefault("is_answering", False)
    st.session_state.setdefault("mode", "read")        # read | answer


_init_session()

res_db = ResourceDB()
classifier = QuestionClassifier()

LOGIN_STATE_PATH = Path("data/login_state.json")


def _collect_in_subprocess(payload: dict, on_progress=None):
    """启动独立采集进程，边跑边把日志转发到面板。

    为什么不直接在当前进程跑：Streamlit(Windows) 会把事件循环策略切成
    Selector，Selector 循环不支持创建子进程，Playwright 会卡死或直接抛
    NotImplementedError。独立进程由系统默认的 Proactor 循环启动，稳定可用。

    on_progress: 每收到一行日志回调一次，用于刷新页面上的实时日志。
    """
    payload_path = Path("data/.collect_payload.json")
    result_path = Path("data/.collect_result.json")
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result_path.unlink(missing_ok=True)

    proc = subprocess.Popen(
        [sys.executable, "collect_cli.py", str(payload_path), str(result_path)],
        cwd=str(Path(__file__).resolve().parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        },
    )
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if "\t" in line:
            lv, msg = line.split("\t", 1)
            log(msg, lv if lv in LOG_LEVELS else "info")
        else:
            log(line, "debug")
        if on_progress:
            on_progress()
    proc.wait()

    if not result_path.exists():
        raise RuntimeError(f"采集进程异常退出（code={proc.returncode}）")
    rows = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink(missing_ok=True)
    payload_path.unlink(missing_ok=True)

    out = []
    for r in rows:
        q = Question(
            url=r["url"],
            qid=r.get("qid", ""),
            title=r.get("title", ""),
            snippet=r.get("snippet", ""),
            source_query=r.get("source_query", ""),
            source=r.get("source", "zhidao"),
        )
        q.matched_keywords = [k for k in r.get("matched_keywords", "").split(",") if k]
        out.append(q)
    return out

# ---------------------------------------------------------------------------
# Sidebar —— 配置面板
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("⚙️ 配置中心")
    st.caption("所有设置会自动保存到 data/settings.json")

    settings = st.session_state["settings"]

    # 运行模式
    st.markdown("#### 🧭 运行模式")
    mode_label = st.radio(
        "选择模式",
        ["📖 只读分析", "🤖 答题模式"],
        index=0 if st.session_state["mode"] == "read" else 1,
        label_visibility="collapsed",
    )
    mode = "read" if mode_label.startswith("📖") else "answer"
    st.session_state["mode"] = mode

    if mode == "read":
        st.info("只采集 + 分析问题，**无需登录**。适合先调研都长什么样。")
    else:
        st.warning("需登录后才能提交回答。")

    # 登录状态指示器 + 手动登录按钮（仅答题模式）
    if mode == "answer":
        st.markdown("#### 🔐 登录状态")
        if LOGIN_STATE_PATH.exists():
            st.success("✅ 已保存登录会话")
            st.caption(f"文件: {LOGIN_STATE_PATH.name}")
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
            st.caption("仅当「手动登录」无法使用时才需要填这个")
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
    source_options = {"zhidao": "百度知道", "tieba": "百度贴吧"}
    picked = st.multiselect(
        "检索来源",
        options=list(source_options.keys()),
        default=[s for s in settings.get("sources", ["zhidao"]) if s in source_options],
        format_func=lambda k: source_options[k],
        help="来源越多请求越多，越容易被百度风控",
    )
    settings["sources"] = picked or ["zhidao"]
    config.save(settings)
    queries_input = st.text_area(
        "每行一个",
        value="\n".join(settings.get("search_queries", [])),
        height=100,
        help="建议每次 3-5 个词，词越多越容易触发百度安全验证",
    )
    queries = [q.strip() for q in queries_input.split("\n") if q.strip()]
    settings["search_queries"] = queries
    config.save(settings)

    # 节流参数 —— 注意 streamlit 1.67 要求 value/step/min/max 类型一致
    st.markdown("#### 🕐 节流参数")
    settings["max_questions_per_query"] = st.number_input(
        "每个搜索词最多采集",
        min_value=5, max_value=100, step=5,
        value=int(settings.get("max_questions_per_query", 15)),
        help="值越大单次请求返回的结果越多，越容易被百度风控。建议 10-20",
    )
    settings["request_interval"] = st.number_input(
        "检索请求间隔（秒）",
        min_value=1.0, max_value=30.0, step=1.0,
        value=float(settings.get("request_interval", 8.0)),
        help="建议 8-10 秒。低于 5 秒连续搜索很容易触发百度安全验证",
    )
    if mode == "answer":
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
    st.write(f"运行模式: **{'📖 只读' if mode == 'read' else '🤖 答题'}**")
    st.write(f"资源库: **{len(res_db.resources)}** 条")
    if mode == "answer":
        st.write(f"已答问题计数: **{len(Answerer._load_answered())}** 条")

# ---------------------------------------------------------------------------
# 日志系统 —— 结构化分级日志
# ---------------------------------------------------------------------------

LOG_LEVELS = {
    "debug": ("🔍", "调试"),
    "info": ("ℹ️", "信息"),
    "success": ("✅", "成功"),
    "warn": ("⚠️", "警告"),
    "error": ("❌", "错误"),
}

# 用模块级缓冲区而不是 session_state：后台线程写日志时更安全
_LOG_LOCK = threading.Lock()
LOG_BUFFER: list = []


def log(msg: str, level: str = "info"):
    """追加一条结构化日志，level ∈ debug/info/success/warn/error"""
    with _LOG_LOCK:
        LOG_BUFFER.append({
            "ts": time.time(),
            "level": level if level in LOG_LEVELS else "info",
            "msg": msg,
        })
        if len(LOG_BUFFER) > 1000:
            del LOG_BUFFER[:-1000]


def _fmt_log(entry: dict) -> str:
    icon, _ = LOG_LEVELS[entry["level"]]
    t = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
    return f"{t} {icon} {entry['msg']}"


@st.fragment(run_every=2.0)
def render_logs(key: str, height: int = 320):
    """渲染日志面板：级别过滤 + 下载（每 2 秒自动刷新，采集/答题时可实时看进度）"""
    with _LOG_LOCK:
        logs = list(LOG_BUFFER)
    if not logs:
        st.caption("暂无日志")
        return

    # 统计各等级条数
    counts = {}
    for e in logs:
        counts[e["level"]] = counts.get(e["level"], 0) + 1
    summary = " | ".join(
        f"{LOG_LEVELS[lv][0]} {LOG_LEVELS[lv][1]} {counts.get(lv, 0)}"
        for lv in ["info", "success", "warn", "error", "debug"]
        if counts.get(lv)
    )
    st.caption(f"共 {len(logs)} 条　{summary}")

    selected = st.multiselect(
        "日志级别",
        options=list(LOG_LEVELS.keys()),
        default=list(LOG_LEVELS.keys()),   # 默认全部显示，含调试明细
        format_func=lambda lv: f"{LOG_LEVELS[lv][0]} {LOG_LEVELS[lv][1]}",
        key=f"{key}_levels",
        label_visibility="collapsed",
    )

    filtered = [e for e in logs if e["level"] in selected]
    text = "\n".join(_fmt_log(e) for e in filtered[-400:]) or "（当前过滤条件下无日志）"
    st.text_area(
        "日志内容", value=text, height=height, disabled=True,
        key=f"{key}_text", label_visibility="collapsed",
    )

    c1, c2 = st.columns([1, 3])
    with c1:
        st.download_button(
            "⬇️ 导出日志",
            data="\n".join(_fmt_log(e) for e in logs).encode("utf-8"),
            file_name=f"log_{time.strftime('%Y%m%d_%H%M')}.txt",
            mime="text/plain",
            key=f"{key}_dl",
            use_container_width=True,
        )
    with c2:
        if st.button("🧹 清空日志", key=f"{key}_clear", use_container_width=True):
            with _LOG_LOCK:
                LOG_BUFFER.clear()
            st.rerun()


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
        do_collect = st.button(
            "🚀 开始采集",
            disabled=st.session_state["is_collecting"],
            type="primary",
            use_container_width=True,
        )
        do_clear = st.button("🗑️ 清空结果", use_container_width=True)
    with col2:
        status_ph = st.empty()
        stream_ph = st.empty()

    if do_clear:
        st.session_state["questions"] = []
        st.session_state["resource_qs"] = []
        log("已清空采集结果", "info")

    if do_collect:
        st.session_state["is_collecting"] = True
        t_start = time.time()

        def _refresh_stream():
            """把最近 20 条日志实时刷到页面上"""
            with _LOG_LOCK:
                tail = list(LOG_BUFFER)[-20:]
            stream_ph.text("\n".join(_fmt_log(e) for e in tail))

        try:
            log(f"开始采集：{len(settings['search_queries'])} 个搜索词，"
                f"每词上限 {settings['max_questions_per_query']} 条", "info")
            log(f"节流配置：检索间隔 {settings['request_interval']}s", "debug")
            _refresh_stream()

            qs = _collect_in_subprocess(
                {
                    "queries": settings["search_queries"],
                    "max_per_query": settings["max_questions_per_query"],
                    "interval": settings["request_interval"],
                    "headless": True,
                    "browser_dir": settings.get("playwright_browser_path", "playwright-browsers"),
                    "profile_dir": "data/browser_profile",
                    "sources": settings.get("sources", ["zhidao"]),
                },
                on_progress=_refresh_stream,
            )
            log(f"候选问题 {len(qs)} 条（已去重）", "info")

            if qs:
                # 分类
                resource_qs = []
                kw_counter = {}
                ad_counter = {}
                for q in qs:
                    if classifier.is_ad_share(q.title):
                        src_name = SOURCES.get(q.source, {}).get("name", q.source)
                        ad_counter[src_name] = ad_counter.get(src_name, 0) + 1
                    q.is_resource = classifier.is_resource_request(q.title, q.snippet)
                    q.matched_keywords = classifier.matched_keywords(q.title, q.snippet)
                    if q.is_resource:
                        resource_qs.append(q)
                        for kw in q.matched_keywords:
                            kw_counter[kw] = kw_counter.get(kw, 0) + 1

                st.session_state["questions"] = list(qs)
                st.session_state["resource_qs"] = resource_qs

                log(f"分类完成：资源需求 {len(resource_qs)} 条 / 其他 "
                    f"{len(qs) - len(resource_qs)} 条", "success")
                if ad_counter:
                    log("过滤广告分享贴 "
                        + "、".join(f"{k} {v} 条" for k, v in ad_counter.items()), "info")
                if kw_counter:
                    top = sorted(kw_counter.items(), key=lambda x: -x[1])[:8]
                    log("命中关键词 TOP: " + ", ".join(f"{k}×{v}" for k, v in top), "debug")
                if not resource_qs:
                    log("本轮没有识别到求资源的问题，可放宽分类关键词", "warn")
            else:
                st.session_state["questions"] = []
                st.session_state["resource_qs"] = []
                log("未搜索到任何问题：请换更通用的关键词，或稍后重试", "warn")

            log(f"采集结束，总耗时 {time.time() - t_start:.1f}s", "success")
        except Exception as e:
            log(f"采集失败：{type(e).__name__}: {e}", "error")
            import traceback
            log(traceback.format_exc().strip().splitlines()[-1], "debug")
        finally:
            st.session_state["is_collecting"] = False
            _refresh_stream()

    with col2:
        status_ph.write(
            f"已采集 **{len(st.session_state['questions'])}** 条，"
            f"其中资源需求 **{len(st.session_state['resource_qs'])}** 条"
        )

    # 表格展示（按来源分 Tab）
    if st.session_state["questions"]:
        df = pd.DataFrame([q.to_dict() for q in st.session_state["questions"]])
        df["分类"] = df.apply(
            lambda r: "🚫 广告贴" if classifier.is_ad_share(r["title"])
            else ("🔴 资源需求" if r["is_resource"] else "⚪ 其他"),
            axis=1,
        )
        # 汇总
        st.caption(
            "共 **%d** 条 —— %s"
            % (
                len(df),
                "、".join(
                    f"{n[0]} {n[1]}"
                    for n in df["source_name"].value_counts().items()
                ),
            )
        )
        # 全局导出放在前面
        if mode == "read":
            c1, c2 = st.columns(2)
            with c1:
                st.download_button(
                    "⬇️ 下载 CSV（全部来源）",
                    data=df[["分类", "source_name", "title", "source_query", "matched_keywords", "url"]]
                    .rename(columns={
                        "source_name": "来源", "title": "问题标题",
                        "source_query": "搜索词", "matched_keywords": "命中关键词", "url": "链接",
                    }).to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"questions_{time.strftime('%Y%m%d_%H%M')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            with c2:
                st.download_button(
                    "⬇️ 下载 JSON（含正文）",
                    data=json.dumps(
                        [q.to_dict() for q in st.session_state["questions"]],
                        ensure_ascii=False, indent=2,
                    ).encode("utf-8"),
                    file_name=f"questions_{time.strftime('%Y%m%d_%H%M')}.json",
                    mime="application/json",
                    use_container_width=True,
                )

        # 按来源分 Tab
        src_counts = df["source_name"].value_counts()
        tabs = st.tabs([f"📖 {name} ({n})" for name, n in src_counts.items()])
        for tab, (name, _) in zip(tabs, src_counts.items()):
            with tab:
                sub = df[df["source_name"] == name]
                show = sub[["分类", "title", "source_query", "matched_keywords", "url"]].rename(
                    columns={"title": "问题标题", "source_query": "搜索词",
                             "matched_keywords": "命中关键词", "url": "链接"}
                )
                st.dataframe(
                    show, use_container_width=True, hide_index=True,
                    column_config={"链接": st.column_config.LinkColumn()},
                )
                # 只读模式支持按来源单独导出
                if mode == "read":
                    st.download_button(
                        f"⬇️ 导出 {name} CSV",
                        data=show.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"{name}_{time.strftime('%Y%m%d_%H%M')}.csv",
                        mime="text/csv",
                        key=f"dl_src_{name}",
                        use_container_width=True,
                    )
    else:
        st.info("还没采集到数据，点左侧按钮开始吧~")

    # 日志
    with st.expander("📋 运行日志", expanded=False):
        render_logs("tab1_logs", height=300)


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
def _render_answer_tab():
    st.header("🚀 自动答题")
    st.caption(
        "对已采集的资源需求问题，用资源库匹配生成答案，再用 Playwright 模拟浏览器提交。"
        "**当前仅支持百度知道**的自动答题；贴吧等其他平台暂未接入，需手动发帖。"
    )

    # 第一步：匹配
    st.subheader("① 资源匹配预览")
    if not st.session_state.get("resource_qs"):
        st.warning("⚠️ 还没有可答的问题，先去「问题检索」Tab 跑一次采集吧")
    else:
        # 答题目前只支持 zhidao，其他来源的问题只展示但不进入答题
        source_map = {"zhidao": "百度知道", "tieba": "百度贴吧"}
        can_answer_qs = [q for q in st.session_state["resource_qs"] if q.source == "zhidao"]
        blocked_qs = [q for q in st.session_state["resource_qs"] if q.source != "zhidao"]
        if blocked_qs:
            st.warning(
                f"⚠️ 有 {len(blocked_qs)} 条问题来自"
                f"{'、'.join({source_map.get(q.source, q.source) for q in blocked_qs})}"
                f"，自动答题暂不支持，将跳过。"
            )

        pairs = []
        for q in can_answer_qs:
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

    st.divider()

    # 开始答题
    st.subheader("③ 开始答题")
    col1, col2 = st.columns([1, 3])

    with col1:
        has_login = LOGIN_STATE_PATH.exists() or bool(settings.get("baidu_cookie"))
        can_answer = has_login and bool(pairs)
        if not has_login:
            st.warning("⚠️ 先在左侧完成登录（点「手动登录」按钮）")

        if st.button("🚀 开始自动答题", disabled=not can_answer or st.session_state["is_answering"],
                     type="primary", use_container_width=True):
            st.session_state["is_answering"] = True
            st.session_state["answer_logs"] = []

            def _run():
                t_start = time.time()
                ans = Answerer(
                    cookie=settings["baidu_cookie"],
                    browser_path=settings.get("playwright_browser_path", "playwright-browsers"),
                    headless=settings["playwright_headless"],
                    max_per_question=settings["max_answers_per_question"],
                    log_callback=lambda m: log(
                        m,
                        "warn" if any(k in m for k in ("[fail]", "[skip]"))
                        else "error" if "[error]" in m
                        else "debug",
                    ),
                )
                pairs_now = list(st.session_state["matched_pairs"])
                total = len(pairs_now)
                ok = 0
                try:
                    log(f"启动浏览器（headless={settings['playwright_headless']}）", "info")
                    ans.start()
                    log(f"共 {total} 条待答，间隔 {settings['answer_interval']}s", "info")
                    for i, (q, res, score) in enumerate(pairs_now, 1):
                        text = res.build_answer_text(q.title)
                        log(f"[{i}/{total}] qid={q.qid or '未知'} | 资源《{res.category}》"
                            f"| 匹配 {score} 分", "info")
                        t_one = time.time()
                        log_obj = ans.answer(q.url, q.qid, text)
                        st.session_state["answer_logs"].append(log_obj)
                        if log_obj.success:
                            ok += 1
                            log(f"[{i}/{total}] 提交成功，用时 {time.time() - t_one:.1f}s", "success")
                        else:
                            log(f"[{i}/{total}] 提交失败或跳过，用时 {time.time() - t_one:.1f}s", "error")
                        if i < total:
                            log(f"等待 {settings['answer_interval'] + 2}s 后继续", "debug")
                            time.sleep(settings["answer_interval"] + 2)
                    log(f"答题结束：成功 {ok}/{total}，总耗时 {time.time() - t_start:.1f}s", "success")
                except Exception as e:
                    log(f"答题异常：{type(e).__name__}: {e}", "error")
                finally:
                    ans.stop()
                    st.session_state["is_answering"] = False

            threading.Thread(target=_run, daemon=True).start()

        if st.button("⏹️ 停止", disabled=not st.session_state["is_answering"], use_container_width=True):
            # 注：线程里的 playwright 没法中途优雅停，这个按钮只做软提示
            st.warning("当前这条答完会停（线程安全考虑，建议等它跑完）")

    with col2:
        if st.session_state["is_answering"]:
            st.info("⏳ 答题进行中...实时日志在下方")
        render_logs("tab3_logs", height=320)

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


with tab3:
    if mode == "read":
        st.header("🚀 自动答题")
        st.info("当前是 **只读分析模式**，答题功能已关闭。")
        st.markdown("""
**两种模式的区别**

| 模式 | 需要登录 | 能做什么 |
|---|---|---|
| 📖 只读分析 | ❌ | 采集问题、分类、资源匹配预览、导出 CSV/JSON |
| 🤖 答题模式 | ✅ | 以上全部 + 用 Playwright 模拟浏览器自动提交回答 |

切换到左侧「🤖 答题模式」并完成登录后，这里会显示匹配预览、Dry Run 和答题按钮。
""")
    else:
        _render_answer_tab()

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
