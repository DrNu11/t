#!/usr/bin/env python3
"""
Trident Agent MVP — 离线可视化复盘面板
================================================

Streamlit + Plotly 专业的量化回测看板，用于客户演示和交易复盘。

功能：
  - 读取 trident_signals.xlsx 信号文件
  - 自动拉取币安 XAUUSDT 1分钟级K线数据
  - 标注新闻发布时刻、入场价、最大浮盈/浮亏
  - 展示 Kimi K3 主模型及 4 个子模型 (DeepSeek/Gemini/Grok/ChatGPT) 的AI推导逻辑

运行：
  cd dashboard
  streamlit run app.py --server.port 8501 --server.address 127.0.0.1
"""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from kline_sources import fetch_binance_klines, fetch_yahoo_klines, normalize_asset, resolve_kline_route

# -----------------------------------------------------------------------------
# Page Config — Dark Theme + Wide Mode
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="Trident 量化复盘看板",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------------------------------------------------------
# Custom CSS — Dark Theme Professional Styling
# -----------------------------------------------------------------------------

st.markdown(
    """
    <style>
    /* Dark theme base */
    :root {
        --bg-primary: #0e1117;
        --bg-secondary: #151a23;
        --text-primary: #e8eaf0;
        --text-secondary: #9fa6b2;
        --accent-green: #00d26a;
        --accent-red: #f87171;
        --accent-blue: #3b82f6;
        --accent-gold: #fbbf24;
        --border-color: #2d3342;
    }

    /* Main container */
    .main {
        background-color: var(--bg-primary);
        color: var(--text-primary);
    }

    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: var(--bg-secondary);
        border-right: 1px solid var(--border-color);
    }

    /* Headers */
    h1, h2, h3 {
        color: var(--text-primary) !important;
        font-weight: 600;
    }

    h1 {
        font-size: 2rem;
        margin-bottom: 1rem;
    }

    /* Cards */
    .signal-card {
        background-color: var(--bg-secondary);
        border: 1px solid var(--border-color);
        border-radius: 12px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    }

    /* Metric values */
    [data-testid="stMetricValue"] {
        font-size: 1.5rem;
        font-weight: 600;
    }

    /* Selectbox */
    .stSelectbox > div > div {
        background-color: var(--bg-secondary);
        color: var(--text-primary);
    }

    /* Date input */
    [data-testid="stDateInput"] {
        background-color: var(--bg-secondary);
    }

    /* File upload */
    [data-testid="stFileUploadUploader"] {
        background-color: var(--bg-secondary);
    }

    /* AI reasoning boxes */
    .ai-reasoning {
        background-color: #1a1f2e;
        border-left: 4px solid var(--accent-blue);
        padding: 1rem 1.25rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        line-height: 1.6;
    }

    .kimi-reasoning {
        border-left-color: #8b5cf6; /* Purple for Kimi K3 */
    }

    /* Sub-model compact cards (DeepSeek / Gemini / Grok / ChatGPT) */
    .sub-model-card {
        background-color: #1a1f2e;
        border-left: 3px solid #3b82f6;
        padding: 0.6rem 1rem;
        border-radius: 6px;
        margin: 0.4rem 0;
        font-size: 0.85rem;
        line-height: 1.6;
        color: #e0e0e0 !important;
    }

    .doubao-reasoning   { border-left-color: #f97316; }
    .deepseek-reasoning { border-left-color: #14b8a6; }
    .gemini-reasoning   { border-left-color: #60a5fa; }
    .grok-reasoning     { border-left-color: #facc15; }
    .chatgpt-reasoning  { border-left-color: #34d399; }

    .sub-model-card * {
        color: #e0e0e0 !important;
        -webkit-text-fill-color: #e0e0e0 !important;
    }

    /* Direction badges */
    .badge-long {
        background-color: rgba(0, 210, 106, 0.2);
        color: var(--accent-green);
        padding: 0.25rem 0.75rem;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.875rem;
    }

    .badge-short {
        background-color: rgba(248, 113, 113, 0.2);
        color: var(--accent-red);
        padding: 0.25rem 0.75rem;
        border-radius: 6px;
        font-weight: 600;
        font-size: 0.875rem;
    }

    /* News title highlight */
    .news-title {
        color: var(--accent-gold);
        font-size: 0.95rem;
        line-height: 1.5;
    }

    /* Asset tag */
    .asset-tag {
        background-color: rgba(59, 130, 246, 0.2);
        color: var(--accent-blue);
        padding: 0.25rem 0.75rem;
        border-radius: 6px;
        font-size: 0.875rem;
        font-weight: 600;
    }

    /* Plotly chart dark theme */
    .js-plotly-plot {
        background-color: var(--bg-secondary);
    }

    /* Compact one-page board */
    html, body, [data-testid="stAppViewContainer"] {
        height: 100%;
        overflow: hidden;
    }
    [data-testid="stAppViewContainer"] > .main {
        height: 100vh;
        overflow: hidden;
    }
    .main .block-container {
        padding: 0.45rem 0.8rem 0.35rem 0.8rem !important;
        max-width: 100% !important;
        height: calc(100vh - 0.2rem);
        overflow: hidden;
    }
    [data-testid="stVerticalBlock"] { gap: 0.35rem !important; }
    [data-testid="stHorizontalBlock"] { gap: 0.45rem !important; }
    h1 { font-size: 1.15rem !important; margin: 0 0 0.15rem 0 !important; }
    h2, h3 { font-size: 0.82rem !important; margin: 0 0 0.2rem 0 !important; }
    [data-testid="stMetricValue"] { font-size: 1.02rem !important; }
    [data-testid="stMetricLabel"] { font-size: 0.68rem !important; }
    [data-testid="stCaption"] { font-size: 0.72rem !important; }
    div[data-testid="stExpander"] { margin-top: 0.2rem; }
    hr { margin: 0.25rem 0 !important; }

    /* Adaptive scrollbars (page + inner grids) */
    ::-webkit-scrollbar {
        width: 7px;
        height: 7px;
    }
    ::-webkit-scrollbar-track {
        background: var(--bg-secondary);
    }
    ::-webkit-scrollbar-thumb {
        background: var(--border-color);
        border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #3d4456;
    }
    * {
        scrollbar-width: thin;
        scrollbar-color: var(--border-color) var(--bg-secondary);
    }

    /* Warning box */
    .warning-box {
        background-color: rgba(251, 191, 36, 0.1);
        border: 1px solid #fbbf24;
        border-radius: 8px;
        padding: 1rem;
        margin: 1rem 0;
    }

    /* ═══════════════════════════════════════════════════════════
       BugFix: 终极样式覆盖
       ═══════════════════════════════════════════════════════════ */

    /* 1. 侧边栏下拉框 — 强制白色文字 */
    section[data-testid="stSidebar"] div[data-baseweb="select"] * {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }

    /* 下拉框选中的值 (输入框内部) */
    section[data-testid="stSidebar"] div[data-baseweb="select"] input {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
        caret-color: #ffffff !important;
    }

    /* 2. 下拉菜单弹出选项 */
    ul[data-baseweb="menu"] li {
        color: #ffffff !important;
        background-color: #1e2130 !important;
    }

    ul[data-baseweb="menu"] li:hover,
    ul[data-baseweb="menu"] li[aria-selected="true"] {
        background-color: #3b82f6 !important;
        color: #ffffff !important;
    }

    /* 3. AI 推导逻辑框 — 强制亮色文字 */
    .ai-reasoning,
    .ai-reasoning *,
    div[data-testid="stMarkdownContainer"] .ai-reasoning * {
        color: #e8eaf0 !important;
        -webkit-text-fill-color: #e8eaf0 !important;
    }

    .kimi-reasoning,
    .kimi-reasoning * {
        color: #e8eaf0 !important;
        -webkit-text-fill-color: #e8eaf0 !important;
    }

    /* 确保 AI 框中任何深色 inline style 都被覆盖 */
    div[data-testid="stMarkdownContainer"] div[style*="background"] * {
        color: #e8eaf0 !important;
        -webkit-text-fill-color: #e8eaf0 !important;
    }

    /* ═══════════════════════════════════════════════════════════
       BugFix: 侧边栏全局亮色覆盖
       ═══════════════════════════════════════════════════════════ */

    /* 1. 侧边栏标签、段落、标题、指标数值 → 亮色 */
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3,
    section[data-testid="stSidebar"] div[data-testid="stMetricValue"] {
        color: #e0e0e0 !important;
    }

    /* 2. 侧边栏下拉框 — 选中值 & 内部元素 */
    section[data-testid="stSidebar"] div[data-testid="stSelectbox"] div[data-baseweb="select"] *,
    section[data-testid="stSidebar"] div[data-testid="stSelectbox"] span {
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }

    /* 3. 侧边栏提示框 (info/success/error) 文字 */
    section[data-testid="stSidebar"] div[data-testid="stAlert"] * {
        color: #ffffff !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# -----------------------------------------------------------------------------
# Asset Mapping — Trident symbol → Binance kline symbol
# -----------------------------------------------------------------------------

ASSET_NAMES: Dict[str, str] = {
    "XAU": "黄金",    "GOLD": "黄金",
    "BTC": "比特币",  "ETH": "以太坊",
    "SOL": "Solana",  "BNB": "BNB",
    "DOGE": "狗狗币", "LTC": "莱特币",
    "LINK": "Chainlink",
    "WTI": "原油",    "OIL": "原油",
}

# -----------------------------------------------------------------------------
# Data Loading Functions
# -----------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def load_signal_data(uploaded_file) -> pd.DataFrame:
    """加载并预处理信号Excel数据。"""
    try:
        df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"读取Excel文件失败: {e}")
        return pd.DataFrame()

    # 标准化列名（处理可能的列名变化）
    column_mapping = {
        "时间": "时间",
        "新闻内容": "新闻内容",
        "品种": "品种",
        "方向": "方向",
        "入场价": "入场价",
        "最高价": "最高价",
        "最低价": "最低价",
        "出场价": "出场价",
        "最大浮盈%": "最大浮盈%",
        "最大浮亏%": "最大浮亏%",
        "评分": "评分",
        "Kimi K3归因": "主模型归因",
        "主模型归因": "主模型归因",
        "大模型归因": "主模型归因",
        "DeepSeek归因": "DeepSeek归因",
        "Gemini归因": "Gemini归因",
        "Grok归因": "Grok归因",
        "ChatGPT归因": "ChatGPT归因",
        "胜负": "胜负",
        "强影响": "强影响",
    }

    # 统一主模型归因等兼容列名后再校验必需列。
    df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})

    # 检查必需的列是否存在
    required_cols = ["时间", "新闻内容", "品种", "方向", "入场价"]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        st.error(f"Excel缺少必需的列: {missing_cols}")
        st.write(f"当前列名: {list(df.columns)}")
        return pd.DataFrame()

    # 清洗数据
    df = df.copy()
    df = df.dropna(subset=["时间", "品种"])

    # 标准化品种代码 — 去掉括号、中文、空格，只保留字母/数字
    def clean_asset(val: str) -> str:
        if pd.isna(val):
            return "UNKNOWN"
        val = str(val).upper().strip()
        # 去掉方括号和中文字符（如 "[BTC-多]" → "BTC"）
        val = re.sub(r'[\[\]（）【】\-_\s]', '', val)
        val = re.sub(r'[一-鿿]+', '', val)
        # 常见别名归一
        alias = {"GOLD": "XAU", "OIL": "WTI", "BITCOIN": "BTC",
                 "ETHEREUM": "ETH", "SOLANA": "SOL", "BNB": "BNB",
                 "DOGE": "DOGE", "LTC": "LTC", "LINK": "LINK"}
        return alias.get(val, val)
    df["品种"] = df["品种"].apply(clean_asset)

    # 标准化方向
    df["方向"] = df["方向"].str.upper().str.strip()
    df = df[df["方向"].isin(["BUY", "SELL", "多", "空", "LONG", "SHORT"])]

    # 提取时间用于显示和K线查询
    df["时间_原始"] = df["时间"].astype(str)  # 保留原始字符串用于显示
    def extract_time(t):
        if pd.isna(t):
            return None
        if isinstance(t, str):
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%H:%M:%S", "%H:%M"]:
                try:
                    dt = datetime.strptime(t.strip(), fmt)
                    return dt.time()  # 只返回时间部分，日期由用户选择器决定
                except ValueError:
                    continue
        return t

    df["时间_提取"] = df["时间"].apply(extract_time)

    # 添加缺失的可选列
    MODEL_COLUMNS = ["主模型归因", "DeepSeek归因", "Gemini归因", "Grok归因", "ChatGPT归因"]
    for col in MODEL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    if "最大浮盈%" not in df.columns:
        df["最大浮盈%"] = None
    if "最大浮亏%" not in df.columns:
        df["最大浮亏%"] = None
    if "评分" not in df.columns:
        df["评分"] = 0.0

    return df


def create_signal_label(row: pd.Series) -> str:
    """创建信号标签，用于Selectbox显示。"""
    asset = row.get("品种", "UNKNOWN")
    direction = row.get("方向", "")
    time_val = str(row.get("时间", ""))
    # 如果有完整日期就显示完整，否则只截 HH:MM:SS
    time_str = time_val[:19] if len(time_val) >= 10 else time_val[:8]
    news = str(row.get("新闻内容", ""))[:40] + "..." if len(str(row.get("新闻内容", ""))) > 40 else str(row.get("新闻内容", ""))

    # 标准化方向显示
    if direction in ["BUY", "多", "LONG"]:
        dir_symbol = "多"
    elif direction in ["SELL", "空", "SHORT"]:
        dir_symbol = "空"
    else:
        dir_symbol = direction

    return f"[{asset}-{dir_symbol}] {time_str} {news}"


# -----------------------------------------------------------------------------
# K线数据获取
# -----------------------------------------------------------------------------

PROXY_URL = os.getenv("HTTP_PROXY", "").strip() or os.getenv("http_proxy", "").strip()


def _resolve_binance_symbol(asset_code: str) -> str:
    """展示用代码。WTI 显示 CL=F，不再当作币安 symbol。"""
    _venue, symbol = resolve_kline_route(asset_code)
    return symbol


def _proxies() -> Optional[Dict[str, str]]:
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


def _to_chart_frame(rows: List[Dict[str, Any]]) -> Optional[pd.DataFrame]:
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
    return df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]


@st.cache_data(ttl=3600)
def fetch_kline_data(
    asset: str,
    event_datetime: datetime,
    before_minutes: int = 30,
    after_minutes: int = 120,
) -> Optional[pd.DataFrame]:
    """加密走币安公共行情（451 自动换 host）；原油只走 Yahoo CL=F。"""
    venue, symbol = resolve_kline_route(asset)
    start_time = event_datetime - timedelta(minutes=before_minutes)
    end_time = event_datetime + timedelta(minutes=after_minutes)
    start_ts = int(start_time.timestamp() * 1000)
    end_ts = int(end_time.timestamp() * 1000)
    errors: List[str] = []
    proxies = _proxies()

    try:
        if venue == "yahoo":
            rows, note = fetch_yahoo_klines(symbol, start_time, end_time, proxies=proxies)
            if rows:
                frame = _to_chart_frame(rows)
                if frame is not None:
                    return frame
            errors.append(note)
        else:
            rows, note = fetch_binance_klines(symbol, start_ts, end_ts, proxies=proxies)
            if rows:
                frame = _to_chart_frame(rows)
                if frame is not None:
                    return frame
            errors.append(note)
            if normalize_asset(asset) in ("XAU", "GOLD"):
                rows, note = fetch_yahoo_klines("GC=F", start_time, end_time, proxies=proxies)
                if rows:
                    frame = _to_chart_frame(rows)
                    if frame is not None:
                        return frame
                errors.append(note)

        st.warning("无法获取该信号时段的K线：" + "；".join(errors[-2:]))
        return None
    except Exception as e:
        st.warning(f"获取K线数据失败: {type(e).__name__}: {e}")
        return None


# -----------------------------------------------------------------------------
# Plotly K线图表绘制
# -----------------------------------------------------------------------------

def create_candlestick_chart(
    kline_df: pd.DataFrame,
    event_time: datetime,
    entry_price: float,
    direction: str,
    symbol_label: str = "",
) -> go.Figure:
    """
    创建专业的K线图表，带事件标注线。

    参数:
        kline_df: K线数据 (带Datetime索引)
        event_time: 事件发生时间
        entry_price: 入场价格
        direction: 交易方向 (BUY/SELL)
        symbol_label: 币安合约代码 (如 XAUUSDT, BTCUSDT)
    """
    if kline_df is None or kline_df.empty:
        # 创建空图表
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            title="无K线数据",
            height=500,
        )
        return fig

    # 确定颜色主题
    if direction in ["BUY", "多", "LONG"]:
        entry_color = "#00d26a"  # Green for long
        event_color = "#fbbf24"  # Gold for news
    else:
        entry_color = "#f87171"  # Red for short
        event_color = "#fbbf24"

    fig = go.Figure()

    # K线图
    fig.add_trace(
        go.Candlestick(
            x=kline_df.index,
            open=kline_df["Open"],
            high=kline_df["High"],
            low=kline_df["Low"],
            close=kline_df["Close"],
            name="K线",
            increasing_line_color="#00d26a",
            decreasing_line_color="#f87171",
        )
    )

    # 成交量（底部柱状图）
    fig.add_trace(
        go.Bar(
            x=kline_df.index,
            y=kline_df["Volume"],
            name="成交量",
            yaxis="y2",
            marker_color="rgba(148, 163, 184, 0.3)",
        )
    )

    # 新闻发布垂直虚线
    fig.add_vline(
        x=event_time,
        line_dash="dash",
        line_width=2,
        line_color=event_color,
        annotation_text="📰 新闻发布",
        annotation_position="top",
        annotation_font_size=12,
        annotation_font_color=event_color,
    )

    # 入场价水平实线
    fig.add_hline(
        y=entry_price,
        line_width=2,
        line_color=entry_color,
        annotation_text=f"💰 入场: {entry_price:.2f}",
        annotation_position="right",
        annotation_font_size=11,
        annotation_font_color=entry_color,
        annotation_bgcolor="rgba(0,0,0,0.7)",
    )

    # 布局设置
    fig.update_layout(
        template="plotly_dark",
        height=550,
        hovermode="x unified",
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False,
        xaxis=dict(
            title="时间（上海时区）",
            gridcolor="#2d3342",
            showgrid=True,
        ),
        yaxis=dict(
            title=f"价格 ({symbol_label})" if symbol_label else "价格",
            gridcolor="#2d3342",
            showgrid=True,
            side="left",
        ),
        yaxis2=dict(
            title="成交量",
            overlaying="y",
            side="right",
            showgrid=False,
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
    )

    return fig


# -----------------------------------------------------------------------------
# Sidebar UI Components
# -----------------------------------------------------------------------------

def render_sidebar(df: pd.DataFrame) -> Tuple[pd.Series, datetime]:
    """渲染侧边栏，返回选中的信号行和完整的日期时间。"""

    st.sidebar.header("⚙️ 数据控制")

    # 文件上传
    uploaded_file = st.sidebar.file_uploader(
        "📁 上传信号文件 (trident_signals.xlsx)",
        type=["xlsx", "xls"],
        help="请上传包含交易信号的Excel文件",
    )

    if uploaded_file is None:
        # 显示示例说明
        st.sidebar.info(
            """
            👋 欢迎使用 Trident 复盘看板！

            请上传 `trident_signals.xlsx` 文件开始分析。

            文件应包含以下列：
            - 时间
            - 新闻内容
            - 品种 (XAU/BTC/WTI)
            - 方向 (多/空)
            - 入场价
            - 最大浮盈%
            - 最大浮亏%
            - 主模型归因（兼容旧列名 Kimi K3归因 / 大模型归因）
            - DeepSeek归因 / Gemini归因 / Grok归因 / ChatGPT归因
            """
        )
        return None, None

    # 加载数据
    df = load_signal_data(uploaded_file)
    if df.empty:
        st.sidebar.error("无法加载信号数据")
        return None, None

    st.sidebar.success(f"✅ 已加载 {len(df)} 条信号")

    # 日期选择器
    st.sidebar.subheader("📅 选择交易日期")
    today = datetime.now().date()
    excel_date = selected_row_date if False else None
    default_date = today
    parsed_dates = [item for item in df.get("日期_提取", []) if item]
    if parsed_dates:
        default_date = parsed_dates[0]
    selected_date = st.sidebar.date_input(
        "选择日期",
        value=default_date,
        max_value=max(today, default_date),
        help="优先使用 Excel 里的完整日期；仅当时分秒时可手动改日期",
    )

    # 创建信号标签列表
    df["信号标签"] = df.apply(create_signal_label, axis=1)

    st.sidebar.subheader("📊 选择交易信号")
    selected_label = st.sidebar.selectbox(
        "点击信号查看详情",
        options=df["信号标签"].tolist(),
        help="格式: [品种-方向] 时间 新闻内容...",
    )

    # 找到选中的行
    selected_row = df[df["信号标签"] == selected_label].iloc[0]

    # K线查询用用户选的日期 + Excel里的时分秒
    time_obj = selected_row.get("时间_提取")
    if time_obj is None:
        st.sidebar.error(f"无法解析时间: {selected_row.get('时间')}")
        return None, None

    event_datetime = datetime.combine(selected_date, time_obj)
    # 显示用的完整原始时间戳
    display_ts = str(selected_row.get("时间_原始", selected_row.get("时间", "")))[:19]

    # 显示信号概要
    st.sidebar.markdown("---")
    st.sidebar.subheader("📋 信号概要")
    st.sidebar.caption(f"🕐 {display_ts}")
    st.sidebar.metric("品种", f"{selected_row['品种']} ({ASSET_NAMES.get(selected_row['品种'], selected_row['品种'])})")
    st.sidebar.metric("方向", "做多" if selected_row["方向"] in ["BUY", "多", "LONG"] else "做空")
    if pd.notna(selected_row.get("入场价")):
        st.sidebar.metric("入场价", f"{selected_row['入场价']:.2f}")

    return selected_row, event_datetime


# -----------------------------------------------------------------------------
# Main View Components
# -----------------------------------------------------------------------------

def render_signal_header(row: pd.Series):
    """渲染信号头部信息。"""
    asset = row.get("品种", "UNKNOWN")
    direction = row.get("方向", "")
    news = row.get("新闻内容", "")

    asset_name = ASSET_NAMES.get(asset, asset)

    # 标准化方向
    if direction in ["BUY", "多", "LONG"]:
        direction_display = "做多"
        badge_class = "badge-long"
    else:
        direction_display = "做空"
        badge_class = "badge-short"

    st.markdown(
        f"""
        <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1rem;">
            <span class="asset-tag">{asset_name} ({asset})</span>
            <span class="{badge_class}">{direction_display}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    ts_str = row.get("时间", "")[:19]
    ts_caption = f" 🕐 {ts_str}" if ts_str else ""
    st.markdown(f"<div class='news-title'>📰 {news}{ts_caption}</div>", unsafe_allow_html=True)


def render_metrics(row: pd.Series):
    """渲染核心指标。"""
    mfe = row.get("最大浮盈%")
    mae = row.get("最大浮亏%")
    score = row.get("评分", 0)

    # 解析百分比字符串（可能是 "2.5%" 格式）
    def parse_pct(val):
        if pd.isna(val):
            return None
        if isinstance(val, str):
            val = val.rstrip("%")
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    mfe_val = parse_pct(mfe)
    mae_val = parse_pct(mae)
    score_val = float(score) if score is not None else 0.0

    col1, col2, col3 = st.columns(3)

    with col1:
        mfe_color = "normal" if mfe_val is None else ("inverse" if mfe_val < 0 else "normal")
        st.metric(
            "最大浮盈 (MFE)",
            f"{mfe_val:+.2f}%" if mfe_val is not None else "—",
            delta_color=mfe_color,
            help="Maximum Favorable Excursion - 最大有利浮动",
        )

    with col2:
        mae_color = "normal" if mae_val is None else ("inverse" if mae_val > 0 else "normal")
        st.metric(
            "最大浮亏 (MAE)",
            f"{mae_val:+.2f}%" if mae_val is not None else "—",
            delta_color=mae_color,
            help="Maximum Adverse Excursion - 最大不利浮动",
        )

    with col3:
        st.metric(
            "共识评分",
            f"{score_val:+.2f}",
            help="AI模型对信号的强度评分（-1.0 到 +1.0）",
        )


# ── 子模型展示配置 ──
_MODEL_CONFIG = {
    "DeepSeek": {"emoji": "🔍", "css_class": "deepseek-reasoning", "label": "DeepSeek"},
    "Gemini":   {"emoji": "💎", "css_class": "gemini-reasoning",   "label": "Gemini"},
    "Grok":     {"emoji": "⚡", "css_class": "grok-reasoning",     "label": "Grok"},
    "ChatGPT":  {"emoji": "🤖", "css_class": "chatgpt-reasoning",  "label": "ChatGPT"},
}

_ACTION_BADGES = {"BUY": "🟢 做多", "SELL": "🔴 做空", "HOLD": "⚪ 观望"}


def _parse_extra_consensus(raw) -> dict:
    """安全解析 extra_models_consensus 字段为 Python dict。"""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    raw_str = str(raw).strip()
    if not raw_str:
        return {}
    try:
        return json.loads(raw_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def render_ai_reasoning(row: pd.Series):
    """渲染 AI 推导逻辑：主模型展开 + extra_models_consensus JSON 拆解为独立折叠卡片。"""
    st.markdown("### 🤖 AI 推导逻辑")

    primary_reasoning = row.get("主模型归因", "")
    sub_models = _parse_extra_consensus(row.get("extra_models_consensus"))

    if not primary_reasoning and not sub_models:
        st.info("该信号暂无AI推导逻辑记录")
        return

    # ── 主模型（始终展开，紫色左边框） ──
    if primary_reasoning:
        st.markdown("#### 🧠 主模型归因")
        cleaned = str(primary_reasoning).strip()
        if len(cleaned) > 2000:
            cleaned = cleaned[:2000] + "\n\n... (内容过长，已截断)"
        st.markdown(
            f"<div class='ai-reasoning kimi-reasoning'>{cleaned}</div>",
            unsafe_allow_html=True,
        )

    # ── 子模型: 逐个渲染折叠卡片 ──
    if sub_models:
        for model_name, model_data in sub_models.items():
            # 提取 action 和 reasoning（兼容 dict / 纯字符串）
            if isinstance(model_data, dict):
                action = (model_data.get("action") or "HOLD").strip().upper()
                reasoning = (model_data.get("reasoning") or "").strip()
            else:
                action = "HOLD"
                reasoning = str(model_data).strip()

            cfg = _MODEL_CONFIG.get(model_name, {})
            emoji = cfg.get("emoji", "🤖")
            css_cls = cfg.get("css_class", "sub-model-card")
            label = cfg.get("label", model_name)
            badge = _ACTION_BADGES.get(action, action)

            if len(reasoning) > 1200:
                reasoning = reasoning[:1200] + "\n\n... (内容过长，已截断)"

            with st.expander(f"{emoji} {label} — {badge}", expanded=False):
                st.markdown(
                    f"<div class='sub-model-card {css_cls}'>"
                    f"<strong>{emoji} {label} · 投票: {badge}</strong>"
                    f"<br><br>{reasoning}"
                    f"</div>",
                    unsafe_allow_html=True,
                )


# -----------------------------------------------------------------------------
# Live paper-trading integration
# -----------------------------------------------------------------------------

PAPER_API_BASE = os.getenv("TRIDENT_API_BASE", "http://127.0.0.1:8000").rstrip("/")


def fetch_live_paper_data() -> Optional[Dict[str, Any]]:
    """读取与 Next.js 模拟盘相同的实时数据源。"""
    try:
        response = requests.get(
            f"{PAPER_API_BASE}/api/replay/positions",
            params={"limit": 200},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "trading_settings" not in payload:
            return None
        return payload
    except (requests.RequestException, ValueError):
        return None


def fetch_news_report() -> Optional[Dict[str, Any]]:
    """读取新闻源报告及其 AI 信号、模拟交易状态。"""
    try:
        response = requests.get(f"{PAPER_API_BASE}/api/news/report", timeout=8)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except (requests.RequestException, ValueError):
        return None


def update_live_paper_settings(is_running: bool, tracks: List[str]) -> Optional[Dict[str, Any]]:
    """通过统一后端接口启动或停止模拟盘。"""
    try:
        response = requests.post(
            f"{PAPER_API_BASE}/api/replay/settings",
            json={"is_running": is_running, "tracks": tracks},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except (requests.RequestException, ValueError):
        return None


def _compact_frame(frame: pd.DataFrame, height: int) -> None:
    """固定高度表格，格内自适应滚动，不撑开整页。"""
    st.dataframe(frame, use_container_width=True, hide_index=True, height=height)


def render_live_paper_dashboard(data: Dict[str, Any]) -> None:
    """展示与网页模拟盘共享的实时行情、仓位、策略和 LLM 统计。"""
    settings = data["trading_settings"]
    account = data.get("account", {})
    feedback = data.get("strategy_feedback", {})
    performance = data.get("llm_performance", {})

    head_left, head_right = st.columns([3, 2])
    with head_left:
        selected_tracks = st.multiselect(
            "投资赛道",
            options=["crypto", "gold", "oil"],
            default=settings.get("tracks", []),
            format_func=lambda value: {"crypto": "加密货币", "gold": "黄金", "oil": "原油"}.get(value, value),
            disabled=bool(settings.get("is_running")),
            label_visibility="collapsed",
        )
    with head_right:
        running = bool(settings.get("is_running"))
        if st.button("停止模拟操盘" if running else "开始模拟操盘", type="secondary" if running else "primary", use_container_width=True):
            if not running and not selected_tracks:
                st.warning("请先选择至少一个投资赛道")
            elif update_live_paper_settings(not running, selected_tracks) is None:
                st.error("后端模拟盘状态更新失败")
            else:
                st.rerun()

    st.caption(
        f"{'运行中' if settings.get('is_running') else '已停止'} · "
        f"{data.get('current_strategy', {}).get('name', '—')} · "
        f"{feedback.get('stage', '—')} · {feedback.get('message', '')} · "
        f"{PAPER_API_BASE} · {data.get('updated_at_ms', '—')}"
    )

    metric_cols = st.columns(5)
    metric_cols[0].metric("当前权益", f"{account.get('current_equity_usdt', 0):,.2f}" if account.get("current_equity_usdt") is not None else "待行情")
    metric_cols[1].metric("已实现", f"{account.get('realized_pnl_usdt', 0):+.2f}")
    metric_cols[2].metric("浮动", f"{account.get('unrealized_pnl_usdt', 0):+.2f}" if account.get("unrealized_pnl_usdt") is not None else "待行情")
    metric_cols[3].metric("持仓", str(sum(not item.get("settled") for item in data.get("positions", []))))
    accuracy = performance.get("accuracy_pct")
    metric_cols[4].metric("LLM 准确率", f"{accuracy:.1f}%" if isinstance(accuracy, (int, float)) else "样本不足")

    pair_col, pos_col = st.columns(2)
    with pair_col:
        st.markdown("**实时交易对**")
        pairs = data.get("pairs", [])
        if pairs:
            pair_df = pd.DataFrame(pairs).rename(columns={
                "symbol": "交易对", "price": "实时价", "change24h": "24h%",
                "source": "行情源", "source_count": "源", "status": "状态",
            })
            visible = [column for column in ["交易对", "实时价", "24h%", "行情源", "源", "状态"] if column in pair_df.columns]
            _compact_frame(pair_df[visible], 168)
        else:
            st.warning("当前赛道暂无行情数据")
    with pos_col:
        st.markdown("**模拟仓位**")
        positions = data.get("positions", [])
        if positions:
            position_df = pd.DataFrame(positions).rename(columns={
                "asset": "品种", "action": "方向", "entry_price": "入场价",
                "current_price": "当前价", "current_pnl_pct": "浮动%",
                "forward_pnl": "结算%", "paper_status": "状态",
            })
            visible = [column for column in ["品种", "方向", "入场价", "当前价", "浮动%", "结算%", "状态"] if column in position_df.columns]
            _compact_frame(position_df[visible], 168)
        else:
            st.info("暂无通过闸门的模拟仓位")

    feedback_rows = feedback.get("latest", [])
    if feedback_rows:
        st.markdown("**策略反馈**")
        feedback_df = pd.DataFrame(feedback_rows).rename(columns={
            "target_asset": "品种", "suggested_action": "方向",
            "evidence_confidence": "置信度", "evidence_action": "证据",
            "trade_gate_reason": "闸门", "created_at": "时间",
        })
        visible = [column for column in ["品种", "方向", "置信度", "证据", "闸门", "时间"] if column in feedback_df.columns]
        _compact_frame(feedback_df[visible], 150)

    st.caption(f"LLM 结算：{performance.get('verdict', '等待真实结算数据')}")
    render_quick_sim_evaluation(data.get("quick_sim") or {})


def render_quick_sim_evaluation(quick: Dict[str, Any]) -> None:
    """展示快速模拟交易与盈利率评测，与 /replay 页面同一份 quick_sim 快照。"""
    if not quick:
        return

    overall = quick.get("overall", {})
    st.markdown("**快速模拟 · 盈利率**")
    if not quick.get("enabled"):
        st.warning("快速模拟评测已关闭（QUICK_SIM_ENABLED=0）")
        return
    st.caption(
        f"{quick.get('horizon_minutes', 0)} 分钟 · {quick.get('notional_usdt', 0):,.0f}U · "
        f"{quick.get('method', '')}"
    )

    winrate = overall.get("winrate_pct")
    roi = overall.get("return_on_notional_pct")
    metric_cols = st.columns(5)
    metric_cols[0].metric("已结算", overall.get("settled", 0))
    metric_cols[1].metric("持仓中", quick.get("open_trades", 0))
    metric_cols[2].metric("胜率", f"{winrate:.1f}%" if isinstance(winrate, (int, float)) else "样本不足")
    metric_cols[3].metric("累计 PnL", f"{overall.get('total_pnl_pct', 0.0):+.4f}%")
    metric_cols[4].metric(
        "盈利率",
        f"{roi:+.4f}%" if isinstance(roi, (int, float)) else "样本不足",
        f"{overall.get('total_pnl_usdt', 0.0):+.2f}U",
    )

    group_col, trade_col = st.columns(2)
    with group_col:
        group_rows = []
        for key, label in (("gate_passed", "闸门通过"), ("gate_rejected", "闸门未过")):
            stats = quick.get(key, {})
            group_rows.append({
                "分组": label,
                "结算": stats.get("settled", 0),
                "胜": stats.get("wins", 0),
                "负": stats.get("losses", 0),
                "胜率%": stats.get("winrate_pct"),
                "PnL%": stats.get("total_pnl_pct"),
            })
        for item in quick.get("by_asset", []):
            group_rows.append({
                "分组": item.get("asset"),
                "结算": item.get("settled", 0),
                "胜": item.get("wins", 0),
                "负": item.get("losses", 0),
                "胜率%": item.get("winrate_pct"),
                "PnL%": item.get("total_pnl_pct"),
            })
        _compact_frame(pd.DataFrame(group_rows), 150)

    with trade_col:
        recent = list(quick.get("open") or []) + list(quick.get("recent") or [])
        if recent:
            recent_df = pd.DataFrame(recent).rename(columns={
                "asset": "品种", "action": "方向", "entry_price": "入场",
                "exit_price": "出场", "pnl_pct": "PnL%", "verdict": "结果",
                "gate_passed": "闸门", "settled": "结算",
            })
            visible = [column for column in ["品种", "方向", "入场", "出场", "PnL%", "结果", "闸门", "结算"] if column in recent_df.columns]
            _compact_frame(recent_df[visible], 150)
        else:
            st.info("等待方向性信号；无真实行情不建仓、不结算。")


def render_news_signal_metrics(report: Dict[str, Any]) -> None:
    """左栏上方：新闻源与闸门指标。"""
    pipeline = report.get("pipeline", {})
    sources = report.get("sources", {})
    source_cols = st.columns(2)
    for column, key, label in zip(source_cols, ("techflow", "eastmoney"), ("TechFlow", "东方财富")):
        payload = sources.get(key, {})
        with column:
            status = str(payload.get("status", "unavailable")).upper()
            st.metric(label, f"{len(payload.get('items', []))} 条", status)

    metric_cols = st.columns(6)
    metric_cols[0].metric("报告", pipeline.get("source_items", 0))
    metric_cols[1].metric("入库", pipeline.get("stored_reports", 0))
    metric_cols[2].metric("已分析", pipeline.get("analyzed", 0))
    metric_cols[3].metric("信号", pipeline.get("signals", 0))
    metric_cols[4].metric("证据过/拒", f"{pipeline.get('evidence_passed', 0)}/{pipeline.get('evidence_rejected', 0)}")
    metric_cols[5].metric("成交/开/结", f"{pipeline.get('executed', 0)}/{pipeline.get('open_trades', 0)}/{pipeline.get('settled_trades', 0)}")


def render_replay_kline(selected_row: Optional[pd.Series], event_datetime: Optional[datetime]) -> None:
    """左侧主区：Excel 信号对应的 K 线与买入线。"""
    st.markdown("**K线 · 买入线**")
    if selected_row is None or event_datetime is None:
        st.info("上传 Excel 并选择信号后，这里显示 K 线和买入线")
        return

    asset = selected_row.get("品种")
    try:
        entry_price = float(selected_row.get("入场价") or 0)
    except (TypeError, ValueError):
        entry_price = 0.0
    direction = selected_row.get("方向")
    symbol = _resolve_binance_symbol(str(asset or ""))
    st.caption(f"{asset} {direction} · {event_datetime:%Y-%m-%d %H:%M:%S} · {symbol} · 入场 {entry_price or '—'}")
    kline_df = fetch_kline_data(str(asset or ""), event_datetime)
    if kline_df is None or kline_df.empty:
        st.warning("无法获取该信号时段的K线")
        return
    fig = create_candlestick_chart(
        kline_df, event_datetime, entry_price, str(direction or ""),
        symbol_label=symbol,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_news_detail_tables(report: Dict[str, Any]) -> None:
    """闸门原因与新闻明细，放在 K 线下方，避免占掉主图。"""
    gate_col, report_col = st.columns([1, 2])
    with gate_col:
        st.markdown("**闸门原因**")
        gate_reasons = report.get("gate_reasons", {})
        if gate_reasons:
            reason_df = pd.DataFrame(
                [{"闸门结论": key, "条数": value} for key, value in gate_reasons.items()]
            ).sort_values("条数", ascending=False)
            _compact_frame(reason_df, 150)
        else:
            st.caption("暂无闸门拒绝记录")
    with report_col:
        st.markdown("**新闻报告明细**")
        reports = report.get("reports", [])
        if not reports:
            st.info("新闻源正在采集")
            return
        report_df = pd.DataFrame(reports).rename(columns={
            "timestamp": "时间", "source": "源", "content": "报告",
            "status": "状态", "asset": "品种", "action": "信号",
            "score": "评分", "evidence_confidence": "置信度",
            "trade_gate_reason": "闸门", "entry_price": "入场",
            "is_correct": "结果", "forward_pnl": "PnL%",
        })
        visible = [column for column in ["时间", "源", "品种", "信号", "评分", "置信度", "闸门", "入场", "结果", "PnL%", "报告"] if column in report_df.columns]
        _compact_frame(report_df[visible], 150)


def main():
    """主应用逻辑：一屏多格，格内滚动，整页仅在溢出时出现自适应滚动条。"""
    st.sidebar.caption("实时看板每 15 秒自动拉取 FastAPI")
    if hasattr(st, "autorefresh"):
        st.autorefresh(interval=15_000, key="live_paper_autorefresh")

    title_col, hint_col = st.columns([2, 3])
    with title_col:
        st.title("Trident 实时看板")
    with hint_col:
        st.caption("新闻链 · 模拟盘 · 快速评测 同屏多格 · 表格内部滚动 · 页面仅溢出时出现滚动条")

    if st.session_state.get("uploaded_file") is None:
        st.session_state["uploaded_file"] = True
    selected_row, event_datetime = render_sidebar(pd.DataFrame())
    news_report = fetch_news_report()
    live_data = fetch_live_paper_data()

    news_col, live_col = st.columns(2, gap="small")
    with news_col:
        st.markdown("**新闻源 · 信号链**")
        if news_report is not None:
            render_news_signal_metrics(news_report)
        else:
            st.warning("新闻报告接口暂不可用")
        render_replay_kline(selected_row, event_datetime)
        if news_report is not None:
            render_news_detail_tables(news_report)
    with live_col:
        st.markdown("**模拟盘 · 盈利率**")
        if live_data is not None:
            render_live_paper_dashboard(live_data)
        else:
            st.warning("模拟盘接口暂不可用")

    if selected_row is not None:
        with st.expander("信号推理详情", expanded=False):
            render_ai_reasoning(selected_row)


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
