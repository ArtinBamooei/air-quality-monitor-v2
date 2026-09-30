from contextlib import closing
from pathlib import Path
import sqlite3
import html
import json

import pandas as pd
import plotly.express as px
import streamlit as st
import os
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.database import connect  # noqa: E402
from src.analytics import european_aqi  # noqa: E402
from src.settings import SETTINGS  # noqa: E402
DB = Path(os.getenv("AIR_QUALITY_DB", str(ROOT / "air_quality_v2.db")))
CITIES_PATH = ROOT / "config" / "cities.json"
POLLUTANTS = {
    "pm2_5": ("PM2.5", "µg/m³"), "pm10": ("PM10", "µg/m³"),
    "aqi": ("ردهٔ ساعتی EEA (۱ تا ۶)", "رده"), "no2": ("دی‌اکسید نیتروژن", "µg/m³"),
    "o3": ("ازون", "µg/m³"), "so2": ("دی‌اکسید گوگرد", "µg/m³"),
    "co": ("مونوکسید کربن", "µg/m³"),
}
COUNTRY_FA = {"Iran": "ایران", "Austria": "اتریش", "Germany": "آلمان"}
COLORS = {"Iran": "#d8a64f", "Austria": "#45a487", "Germany": "#6684d9"}

st.set_page_config(page_title="نَفَس | کیفیت هوای شهرها", page_icon="◉", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Vazirmatn:wght@400;500;600;700;800&display=swap');
:root{--ink:#1d302d;--muted:#75817b;--canvas:#f4f5f1;--line:#e5e8e1;--forest:#183e35;--green:#35775e;--lime:#d9ec9c;--white:#fff;--amber:#f2d8a3}
html,body,[class*="css"]{font-family:'Vazirmatn',sans-serif;color:var(--ink)}
.stApp{background:var(--canvas);direction:rtl}
[data-testid="stSidebar"]{background:#edf0e9;border-left:1px solid var(--line)}
.block-container{max-width:1420px;padding:1.5rem 2.15rem 2.6rem}
[data-testid="stSidebar"] .block-container{padding:1.2rem 1rem}
.brand{display:flex;align-items:center;gap:11px;padding:.25rem .3rem 1.15rem;border-bottom:1px solid #dce2d8;margin-bottom:1rem}
.brand-mark{display:grid;place-items:center;width:39px;height:39px;background:var(--forest);color:var(--lime);border-radius:13px;font-size:20px;box-shadow:0 5px 13px #173d3428}
.brand-name{font-size:18px;font-weight:800;letter-spacing:-.04em}.brand-sub{font:10px 'DM Mono',monospace;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;direction:ltr;text-align:right}
.side-copy{font-size:11px;line-height:1.9;color:var(--muted);padding:.5rem .2rem}
.hero{position:relative;overflow:hidden;display:flex;justify-content:space-between;align-items:center;gap:16px;padding:1.75rem 2rem;margin:0 0 1.15rem;border-radius:22px;background:linear-gradient(112deg,#183e35 0%,#245846 65%,#34775c 100%);color:#f6faf4;box-shadow:0 14px 38px #183e351c;animation:rise .45s ease both}
.hero:after{content:"";position:absolute;width:230px;height:230px;border:1px solid #ffffff17;border-radius:50%;left:10%;top:-170px;box-shadow:0 0 0 28px #ffffff06,0 0 0 58px #ffffff04;animation:float 12s ease-in-out infinite alternate}
.hero-copy,.hero-status{position:relative;z-index:1}.hero-kicker{font:10px 'DM Mono',monospace;letter-spacing:.13em;color:#d1e5a5;direction:ltr;text-align:right}.hero h1{font-size:clamp(1.55rem,2.7vw,2.25rem);line-height:1.35;letter-spacing:-.045em;margin:.35rem 0;font-weight:800}.hero p{font-size:12px;color:#d5e2d9;margin:0}.hero-status{display:flex;align-items:center;gap:8px;border:1px solid #ffffff30;background:#ffffff12;border-radius:99px;padding:.55rem .8rem;font-size:10px;white-space:nowrap;backdrop-filter:blur(8px)}
.pulse{width:7px;height:7px;border-radius:50%;background:#d9ec9c;box-shadow:0 0 0 4px #d9ec9c24;animation:pulse 2s ease infinite}
.metric{height:100%;min-height:112px;background:#fff;border:1px solid var(--line);border-radius:17px;padding:1rem 1.1rem;transition:transform .2s ease,box-shadow .2s ease;animation:rise .42s ease both}.metric:hover{transform:translateY(-3px);box-shadow:0 12px 24px #183e3510}.metric-top{display:flex;justify-content:space-between;align-items:center}.metric-label{font-size:11px;color:var(--muted)}.metric-icon{display:grid;place-items:center;width:29px;height:29px;border-radius:10px;background:#eef3e8;color:var(--green);font-size:13px}.metric-value{font:700 25px 'DM Mono','Vazirmatn',sans-serif;letter-spacing:-.055em;line-height:1.35;margin:.38rem 0 .1rem;color:var(--ink)}.metric-note{font-size:10px;color:#89938d}
.section-heading{display:flex;align-items:flex-end;justify-content:space-between;margin:1.3rem .1rem .65rem;gap:10px}.section-title{font-size:15px;font-weight:700;letter-spacing:-.02em}.section-sub{font-size:10px;color:var(--muted);margin-top:2px}.section-tag{font:9px 'DM Mono',monospace;color:#6f7c75;background:#eef2eb;border-radius:99px;padding:.34rem .55rem;white-space:nowrap}
[data-testid="stVerticalBlockBorderWrapper"]{background:#fff;border:1px solid var(--line);border-radius:18px;box-shadow:0 2px 8px #19392b05}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlock"]{gap:.65rem}
.card-title{font-size:13px;font-weight:700;letter-spacing:-.015em}.card-sub{font-size:10px;color:var(--muted);margin-top:-5px}
.empty{padding:2rem 1rem;border:1px dashed #cbd5c8;border-radius:16px;text-align:center;background:#fafbf8;color:var(--muted);margin:.4rem 0}.empty-icon{font-size:24px;color:var(--green);margin-bottom:.35rem}.empty-title{font-size:14px;font-weight:700;color:var(--ink);margin-bottom:.3rem}.empty p{font-size:11px;margin:0}
.footer{padding:.9rem .15rem;color:#87918b;font-size:10px;border-top:1px solid var(--line);margin-top:1.3rem}
div[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:12px;overflow:hidden}
div.stButton>button{border-radius:10px;border:1px solid #d9e0d6;font-family:'Vazirmatn',sans-serif}
@keyframes rise{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}@keyframes pulse{50%{box-shadow:0 0 0 8px #d9ec9c00}}@keyframes float{to{transform:translate(-12px,13px)}}
@media(max-width:800px){.block-container{padding:1rem .8rem 2rem}.hero{padding:1.35rem;border-radius:18px}.hero-status{display:none}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=60)
def load_data(db_path: str, modified: float) -> pd.DataFrame:
    """Load current-status rows only from clean dimensional Gold facts."""
    del modified
    if not Path(db_path).exists():
        return pd.DataFrame()
    try:
        with closing(connect(db_path)) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"fact_air_quality", "fact_air_quality_sources", "dim_city",
                        "dim_pollutant", "dim_provider", "dim_time"}
            if not required.issubset(tables):
                return pd.DataFrame()
            facts = pd.read_sql_query("""SELECT c.city AS city_name,c.country,t.time_key AS timestamp,
                p.pollutant,dp.provider,fs.value,fs.unit,fs.run_id,MAX(sm.observed_at) AS source_observed_at
                FROM fact_air_quality_sources fs
                JOIN fact_air_quality f USING(city_key,pollutant_key,time_key)
                JOIN dim_city c USING(city_key) JOIN dim_pollutant p USING(pollutant_key)
                JOIN dim_time t USING(time_key) JOIN dim_provider dp USING(provider_key)
                JOIN silver_measurements sm ON sm.city=c.city AND sm.provider=dp.provider
                  AND sm.pollutant=p.pollutant AND sm.run_id=fs.run_id
                  AND substr(sm.observed_at,1,13)=substr(t.time_key,1,13)
                  AND sm.value_type='concentration' AND sm.quality_flag='ok'
                GROUP BY c.city,c.country,t.time_key,p.pollutant,dp.provider,fs.value,fs.unit,fs.run_id
                ORDER BY t.time_key DESC LIMIT 250000""", con)
            if facts.empty:
                return pd.DataFrame()
            source_times = pd.to_datetime(facts.source_observed_at, utc=True, errors="coerce")
            age_minutes = (datetime.now(timezone.utc) - source_times).dt.total_seconds() / 60
            limits = facts.provider.map(SETTINGS.freshness_minutes).fillna(90)
            facts = facts[(source_times.notna()) & (age_minutes <= limits)].copy()
            if facts.empty:
                return pd.DataFrame()
            index = ["city_name", "country", "timestamp"]
            grouped = facts.groupby([*index, "pollutant"], as_index=False).agg(
                value=("value", "median"), n_sources=("provider", "nunique"),
                sources=("provider", lambda values: ",".join(sorted(set(values))))
            )
            wide = grouped.pivot_table(index=index, columns="pollutant", values="value", aggfunc="first").reset_index()
            counts = grouped.pivot_table(index=index, columns="pollutant", values="n_sources", aggfunc="first")
            counts.columns = [f"source_count_{name}" for name in counts.columns]
            wide = wide.merge(counts.reset_index(), on=index, how="left")
            source_names = grouped.pivot_table(index=index, columns="pollutant", values="sources", aggfunc="first")
            source_names.columns = [f"sources_{name}" for name in source_names.columns]
            wide = wide.merge(source_names.reset_index(), on=index, how="left")
            wide["source_count"] = wide.get("source_count_pm2_5")
            wide["sources"] = wide.get("sources_pm2_5", "")
            wide["quality_flag"] = "ok"
            aqi_pollutants = ("pm2_5", "pm10", "o3", "no2", "so2")
            wide["aqi"] = wide.apply(lambda row: european_aqi(
                {name: float(row[name]) for name in aqi_pollutants
                 if name in wide.columns and pd.notna(row[name])}), axis=1)
            return wide.sort_values("timestamp", ascending=False)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise RuntimeError(f"Unable to read current Gold facts ({type(exc).__name__})") from exc


@st.cache_data(ttl=60)
def load_history(db_path: str, modified: float) -> pd.DataFrame:
    """Load the long-term mart separately; freshness stays visible by pollutant."""
    del modified
    if not Path(db_path).exists():
        return pd.DataFrame()
    try:
        with closing(connect(db_path)) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "historical_fact_air_quality" not in tables:
                return pd.DataFrame()
            facts = pd.read_sql_query("""SELECT city AS city_name,country,observed_at AS timestamp,
                pollutant,value,quality_flag FROM historical_fact_air_quality
                ORDER BY observed_at DESC LIMIT 250000""", con)
            if facts.empty:
                return pd.DataFrame()
            index = ["city_name", "country", "timestamp"]
            values = facts.pivot_table(index=index, columns="pollutant", values="value", aggfunc="first")
            flags = facts.pivot_table(index=index, columns="pollutant", values="quality_flag", aggfunc="first")
            flags.columns = [f"quality_{name}" for name in flags.columns]
            wide = values.join(flags).reset_index()
            if "air_quality_indices" in tables:
                indices = pd.read_sql_query("""SELECT city AS city_name,country,observed_at AS timestamp,
                    european_aqi AS aqi,quality_flag AS quality_aqi FROM air_quality_indices""", con)
                wide = wide.merge(indices, on=index, how="left")
            return wide.sort_values("timestamp", ascending=False)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise RuntimeError(f"Unable to read historical marts ({type(exc).__name__})") from exc


def load_raw() -> pd.DataFrame:
    if not DB.exists():
        return pd.DataFrame()
    try:
        with closing(connect(DB)) as con:
            bronze_tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "bronze_responses" in bronze_tables:
                return pd.read_sql_query("""SELECT ingested_at, city, country, provider, run_id,
                    http_status, status, error FROM bronze_responses ORDER BY id DESC LIMIT 200""", con)
            available = {row[1] for row in con.execute("PRAGMA table_info(raw_observations)")}
            ingest_column = "ingested_at" if "ingested_at" in available else "fetched_at"
            columns = [name for name in (ingest_column, "observed_at", "city_name", "country", "provider",
                        "station_name", "station_distance_km", "status", "error") if name in available]
            if not columns:
                return pd.DataFrame()
            return pd.read_sql_query(f"SELECT {','.join(columns)} FROM raw_observations ORDER BY fetched_at DESC LIMIT 200", con)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise RuntimeError(f"Unable to read Bronze report ({type(exc).__name__})") from exc


def load_silver() -> pd.DataFrame:
    if not DB.exists():
        return pd.DataFrame()
    try:
        with closing(connect(DB)) as con:
            return pd.read_sql_query("""SELECT observed_at, ingested_at, city, country, provider,
                pollutant, value, unit, value_type, quality_flag, run_id
                FROM silver_measurements ORDER BY id DESC LIMIT 300""", con)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        raise RuntimeError(f"Unable to read Silver sample ({type(exc).__name__})") from exc


def load_quality_status() -> dict:
    empty = {"run_id": None, "status": "بدون اجرا", "rejected": 0, "flagged": 0}
    if not DB.exists():
        return empty
    try:
        with closing(connect(DB)) as con:
            row = con.execute("SELECT run_id,status FROM collection_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            if not row:
                return empty
            counts = con.execute("""SELECT
                COALESCE(SUM(CASE WHEN action IN ('reject','fail') THEN count ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN action IN ('mark_stale','flag','warn') THEN count ELSE 0 END),0)
                FROM dq_results WHERE run_id=?""", (row[0],)).fetchone()
            return {"run_id": row[0], "status": row[1], "rejected": counts[0], "flagged": counts[1]}
    except sqlite3.Error as exc:
        raise RuntimeError(f"Unable to read pipeline status ({type(exc).__name__})") from exc


def latest_timestamp(frame: pd.DataFrame):
    if frame.empty or "timestamp" not in frame:
        return None
    values = pd.to_datetime(frame.timestamp, utc=True, errors="coerce").dropna()
    return values.max() if not values.empty else None


def card_metric(label: str, value: str, note: str, icon: str) -> str:
    return (f'<div class="metric"><div class="metric-top"><div class="metric-label">{label}</div>'
            f'<div class="metric-icon">{icon}</div></div><div class="metric-value">{value}</div>'
            f'<div class="metric-note">{note}</div></div>')


def style_figure(fig, height: int):
    fig.update_layout(height=height, margin=dict(l=5, r=8, t=8, b=5), paper_bgcolor="white",
                      plot_bgcolor="white", font_family="Vazirmatn, sans-serif", font_color="#58655f",
                      hoverlabel=dict(bgcolor="#183e35", font_color="white", font_family="Vazirmatn"),
                      transition=dict(duration=380, easing="cubic-in-out"))
    return fig


# Dashboard view: filters, current conditions, comparisons, trends, and lineage details.
COUNTRY_EMOJI = {"Iran": "🇮🇷", "Austria": "🇦🇹", "Germany": "🇩🇪"}
POLLUTANT_OPTIONS = {"PM2.5": "pm2_5", "PM10": "pm10", "نیتروژن دی‌اکسید": "no2",
                     "ازون": "o3", "گوگرد دی‌اکسید": "so2", "مونوکسید کربن": "co"}


def format_time(value) -> str:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    local = parsed.tz_convert("Asia/Tehran")
    return f"{local.year:04d}/{local.month:02d}/{local.day:02d} · {local.hour:02d}:{local.minute:02d}" if pd.notna(parsed) else "—"


def format_number(value, decimals: int = 1) -> str:
    return f"{float(value):,.{decimals}f}" if pd.notna(value) else "—"


def render_metric(label: str, value: str, note: str, accent: str = "green") -> str:
    safe = [html.escape(str(part)) for part in (label, value, note)]
    return (f'<div class="metric {accent}"><div class="metric-label">{safe[0]}</div>'
            f'<div class="metric-value">{safe[1]}</div><div class="metric-note">{safe[2]}</div></div>')


@st.cache_data(ttl=60)
def read_city_config(path: str) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=["city_name", "country"])
    configured = json.loads(Path(path).read_text(encoding="utf-8"))
    return pd.DataFrame([{"city_name": city["name"], "country": city["country"]} for city in configured])


st.set_page_config(page_title="نَفَس | پایش کیفیت هوا", page_icon="◉", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap');
:root{--ink:#20312d;--muted:#728078;--line:#e5eae3;--paper:#fff;--canvas:#f4f6f2;--forest:#1c473b;--green:#3e8068;--mint:#e8f1e9;--amber:#b78439;--red:#b85d54}
html,body,[class*="css"]{font-family:'Vazirmatn',sans-serif;color:var(--ink)}
.stApp{background:var(--canvas);direction:rtl}.block-container{max-width:1320px;padding:1.35rem 2rem 2.3rem}
[data-testid="stSidebar"]{background:#eef2ec;border-left:1px solid var(--line)}
[data-testid="stSidebar"] .block-container{padding:1.1rem 1rem}
.brand{display:flex;align-items:center;gap:10px;padding:.25rem .15rem 1rem;border-bottom:1px solid #dce4dc;margin-bottom:.8rem}
.brand-mark{display:grid;place-items:center;width:38px;height:38px;border-radius:12px;background:var(--forest);color:#d9edb4;font-size:20px}
.brand-name{font-size:17px;font-weight:800}.brand-sub{font-size:9px;color:var(--muted);letter-spacing:.12em;direction:ltr}
.side-note{font-size:11px;color:var(--muted);line-height:1.9;padding:.45rem .15rem}
.hero{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:1.55rem 1.8rem;margin-bottom:1.05rem;border-radius:19px;background:linear-gradient(112deg,#173e34,#34725a);color:white;box-shadow:0 10px 30px #173e3418;animation:fade-up .35s ease both}
.hero h1{font-size:clamp(1.45rem,2.3vw,2rem);margin:.15rem 0 .25rem;font-weight:800}.hero p{font-size:12px;color:#d8e6dc;margin:0}.hero-kicker{font-size:9px;letter-spacing:.14em;color:#cfe2a5;direction:ltr;text-align:right}
.hero-time{background:#ffffff17;border:1px solid #ffffff35;border-radius:12px;padding:.55rem .8rem;font-size:11px;white-space:nowrap}
.section-head{display:flex;align-items:end;justify-content:space-between;margin:1.3rem .1rem .65rem;gap:10px}.section-title{font-size:15px;font-weight:700}.section-note{font-size:10px;color:var(--muted);margin-top:2px}
.metric{height:100%;min-height:105px;padding:.95rem 1rem;background:var(--paper);border:1px solid var(--line);border-radius:15px;border-top:3px solid var(--green);animation:fade-up .35s ease both}
.metric.amber{border-top-color:var(--amber)}.metric.red{border-top-color:var(--red)}.metric-label{font-size:10px;color:var(--muted)}.metric-value{font-size:24px;font-weight:800;line-height:1.5;margin:.16rem 0;color:var(--ink);direction:rtl}.metric-note{font-size:10px;color:#87928b}
.panel{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:1rem 1.05rem;height:100%}.panel-title{font-size:13px;font-weight:700}.panel-note{font-size:10px;color:var(--muted);margin:.12rem 0 .65rem}
[data-testid="stVerticalBlockBorderWrapper"]{background:var(--paper);border:1px solid var(--line);border-radius:15px}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stButton button{border-radius:9px;font-family:'Vazirmatn',sans-serif}.empty{padding:1.4rem;border:1px dashed #cbd6cb;border-radius:14px;text-align:center;color:var(--muted);background:#fafbf9;font-size:12px}
.footer{margin-top:1.5rem;padding:.8rem .1rem;border-top:1px solid var(--line);color:#87928b;font-size:10px}
@keyframes fade-up{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:760px){.block-container{padding:1rem .75rem 1.5rem}.hero{align-items:flex-start;padding:1.2rem;flex-direction:column}.hero-time{white-space:normal}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}
</style>
""", unsafe_allow_html=True)

st.sidebar.markdown('<div class="brand"><div class="brand-mark">◉</div><div><div class="brand-name">نَفَس</div><div class="brand-sub">AIR QUALITY MONITOR · V2</div></div></div>', unsafe_allow_html=True)
st.sidebar.markdown('<div class="side-note">فیلترهای این بخش روی همهٔ نمودارها و جدول‌ها اعمال می‌شوند.</div>', unsafe_allow_html=True)
if st.sidebar.button("↻ تازه‌سازی داده‌ها", width="stretch"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.markdown('<div class="side-note">دادهٔ داشبورد هر ۶۰ ثانیه بازخوانی می‌شود. دریافت دادهٔ جدید با اجرای collector انجام می‌شود.</div>', unsafe_allow_html=True)

db_modified = DB.stat().st_mtime if DB.exists() else 0.0
try:
    data = load_data(str(DB), db_modified)
    history_data = load_history(str(DB), db_modified)
    run_status = load_quality_status()
    bronze = load_raw()
    silver = load_silver()
    city_config = read_city_config(str(CITIES_PATH))
except (RuntimeError, ValueError, OSError) as exc:
    st.error(f"بارگذاری داشبورد ناموفق بود: {type(exc).__name__}")
    st.stop()

last_seen = latest_timestamp(data if not data.empty else history_data)
last_seen_label = format_time(last_seen) if last_seen is not None else "داده‌ای ثبت نشده"
st.markdown(f'<section class="hero"><div><div class="hero-kicker">AIR QUALITY · CITY MONITORING</div><h1>پایش کیفیت هوای شهرها</h1><p>مرور منظم داده‌های پاک‌سازی‌شده، روندها و وضعیت دریافت از منابع</p></div><div class="hero-time">آخرین زمان مشاهده<br><b>{html.escape(last_seen_label)}</b></div></section>', unsafe_allow_html=True)

status_fa = {"complete":"کامل", "ok":"کامل", "degraded":"کاهش‌یافته", "running":"در حال اجرا", "fail":"ناموفق"}
status_label = status_fa.get(run_status["status"], run_status["status"])
status_accent = "green" if status_label == "کامل" else "amber" if status_label in ("کاهش‌یافته", "در حال اجرا") else "red"
quality_cards = st.columns(3, gap="medium")
quality_values = [("وضعیت آخرین اجرا", status_label, "سلامت اجرای جمع‌آوری", status_accent),
                  ("ردیف‌های ردشده", f"{run_status['rejected']:,}", "در اجرای اخیر", "red"),
                  ("هشدارها و علامت‌ها", f"{run_status['flagged']:,}", "دادهٔ کهنه یا اختلاف منابع", "amber")]
for column, (label, value, note, accent) in zip(quality_cards, quality_values):
    with column:
        st.markdown(render_metric(label, value, note, accent), unsafe_allow_html=True)

if data.empty and not history_data.empty:
    view_data = history_data.copy()
    view_data["quality_flag"] = "stale"
else:
    view_data = data.copy()

configured_cities = city_config.copy()
if not view_data.empty and {"city_name", "country"}.issubset(view_data.columns):
    configured_cities = pd.concat([configured_cities, view_data[["city_name", "country"]]], ignore_index=True).drop_duplicates()

st.markdown('<div class="section-head"><div><div class="section-title">فیلترهای نمایش</div><div class="section-note">یک‌بار انتخاب کن؛ تمام بخش‌های پایین با همان انتخاب به‌روز می‌شوند.</div></div></div>', unsafe_allow_html=True)
filter_cols = st.columns([1, 1.4, 1.2], gap="medium")
with filter_cols[0]:
    countries_available = sorted(configured_cities.country.dropna().unique())
    country_options = ["همهٔ کشورها", *[f"{COUNTRY_EMOJI.get(c, '')} {COUNTRY_FA.get(c, c)}".strip() for c in countries_available]]
    country_label = st.selectbox("کشور", country_options, key="dashboard_country")
    country = None if country_label == "همهٔ کشورها" else countries_available[country_options.index(country_label) - 1]
with filter_cols[1]:
    cities_available = configured_cities if country is None else configured_cities[configured_cities.country.eq(country)]
    city_map = dict(zip(cities_available.city_name, cities_available.city_name))
    city_labels = list(city_map)
    selected_cities = st.multiselect("شهرها", city_labels, default=city_labels, placeholder="انتخاب شهر")
with filter_cols[2]:
    selected_pollutant_label = st.selectbox("آلایندهٔ روند", list(POLLUTANT_OPTIONS), key="dashboard_pollutant")
    selected_pollutant = POLLUTANT_OPTIONS[selected_pollutant_label]

if country is not None and not view_data.empty:
    view_data = view_data[view_data.country.eq(country)]
if not view_data.empty and "city_name" in view_data:
    view_data = view_data[view_data.city_name.isin(selected_cities)]
current_data = view_data[view_data.quality_flag.eq("ok")] if "quality_flag" in view_data else view_data
latest = current_data.drop_duplicates("city_name", keep="first") if not current_data.empty else current_data

st.markdown('<div class="section-head"><div><div class="section-title">خلاصهٔ وضعیت جاری</div><div class="section-note">هر کارت فقط از تازه‌ترین رکورد تمیز هر شهر استفاده می‌کند.</div></div></div>', unsafe_allow_html=True)
active_count = latest.city_name.nunique() if not latest.empty and "city_name" in latest else 0
pm25_values = latest["pm2_5"].dropna() if not latest.empty and "pm2_5" in latest else pd.Series(dtype=float)
aqi_values = latest["aqi"].dropna() if not latest.empty and "aqi" in latest else pd.Series(dtype=float)
source_values = latest["source_count"].dropna() if not latest.empty and "source_count" in latest else pd.Series(dtype=float)
failed_requests = int(bronze.status.isin(["fail", "error"]).sum()) if not bronze.empty and "status" in bronze else 0
metric_cards = st.columns(4, gap="medium")
summary_values = [("شهرهای دارای داده", f"{active_count}/{len(selected_cities)}", "در انتخاب فعلی", "green"),
                  ("میانگین PM2.5", f"{pm25_values.mean():.1f}" if not pm25_values.empty else "—", "µg/m³ · تازه‌ترین ردیف هر شهر", "green"),
                  ("ردهٔ نامطلوب EEA", f"{int(aqi_values.ge(4).sum())}" if not aqi_values.empty else "—", f"از {len(aqi_values)} شهر دارای ردهٔ معتبر", "amber"),
                  ("منابع برای PM2.5", f"{source_values.mean():.1f}/2" if not source_values.empty else "—", f"خطاهای ثبت‌شده در Bronze: {failed_requests}", "green")]
for column, (label, value, note, accent) in zip(metric_cards, summary_values):
    with column:
        st.markdown(render_metric(label, value, note, accent), unsafe_allow_html=True)

st.markdown('<div class="section-head"><div><div class="section-title">مقایسهٔ شهرها</div><div class="section-note">برای مقایسه، آلاینده را از کنترل نمودار انتخاب کن.</div></div></div>', unsafe_allow_html=True)
compare_left, compare_right = st.columns([1.65, 1], gap="medium")
with compare_left:
    with st.container(border=True):
        chart_metric_label = st.selectbox("آلایندهٔ نمودار", list(POLLUTANT_OPTIONS), key="compare_pollutant")
        chart_metric = POLLUTANT_OPTIONS[chart_metric_label]
        chart_data = latest.dropna(subset=[chart_metric]).sort_values(chart_metric) if not latest.empty and chart_metric in latest else pd.DataFrame()
        if chart_data.empty:
            st.markdown('<div class="empty">برای انتخاب فعلی دادهٔ قابل‌مقایسه وجود ندارد.</div>', unsafe_allow_html=True)
        else:
            chart_data = chart_data.copy()
            chart_data["city_display"] = chart_data.apply(lambda row: f"{COUNTRY_EMOJI.get(row['country'], '')} {row['city_name']}", axis=1)
            fig = px.bar(chart_data, x=chart_metric, y="city_display", color="country", orientation="h",
                         color_discrete_map=COLORS, hover_data={"country": True, chart_metric: ":.2f"})
            fig.update_traces(marker_line_width=0, opacity=.9)
            fig.update_layout(height=max(330, min(570, 34 * len(chart_data) + 100)), margin=dict(l=8,r=8,t=12,b=8),
                              paper_bgcolor="white", plot_bgcolor="white", font_family="Vazirmatn",
                              xaxis_title=POLLUTANTS[chart_metric][1], yaxis_title="", legend_title_text="کشور",
                              bargap=.28, transition_duration=300)
            fig.update_yaxes(categoryorder="total ascending", showgrid=False)
            fig.update_xaxes(gridcolor="#edf0eb", zeroline=False)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar":False})
with compare_right:
    with st.container(border=True):
        st.markdown('<div class="panel-title">پوشش منبع‌های PM2.5</div><div class="panel-note">تعداد ارائه‌دهندگان مستقل در آخرین دادهٔ هر شهر</div>', unsafe_allow_html=True)
        coverage = latest.dropna(subset=["source_count"]).sort_values("source_count") if not latest.empty and "source_count" in latest else pd.DataFrame()
        if coverage.empty:
            st.markdown('<div class="empty">اطلاعات پوشش منبع‌ها در دسترس نیست.</div>', unsafe_allow_html=True)
        else:
            coverage = coverage.copy()
            coverage["city_display"] = coverage.apply(lambda row: f"{COUNTRY_EMOJI.get(row['country'], '')} {row['city_name']}", axis=1)
            fig = px.bar(coverage, x="source_count", y="city_display", orientation="h", range_x=[0,2],
                         color="source_count", color_continuous_scale=["#e9eee8","#a8c8a7","#43846a"],
                         hover_data={"sources":True} if "sources" in coverage else None)
            fig.update_layout(height=max(330,min(570,34*len(coverage)+100)), margin=dict(l=8,r=8,t=12,b=8),
                              paper_bgcolor="white",plot_bgcolor="white",font_family="Vazirmatn",
                              coloraxis_showscale=False,xaxis_title="تعداد منبع",yaxis_title="",transition_duration=300)
            fig.update_xaxes(dtick=1,gridcolor="#edf0eb")
            fig.update_yaxes(categoryorder="total ascending",showgrid=False)
            st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})

st.markdown('<div class="section-head"><div><div class="section-title">روند و جزئیات</div><div class="section-note">تاریخچهٔ ثبت‌شده جدا از کارت‌های وضعیت جاری بررسی می‌شود.</div></div></div>', unsafe_allow_html=True)
trend_col, detail_col = st.columns([1,1.35], gap="medium")
with trend_col:
    with st.container(border=True):
        st.markdown(f'<div class="panel-title">روند {html.escape(selected_pollutant_label)}</div><div class="panel-note">شهر منتخب · زمان تهران</div>', unsafe_allow_html=True)
        available_trend_cities = [city for city in selected_cities if not history_data.empty and city in set(history_data.city_name)]
        if available_trend_cities:
            trend_city = st.selectbox("شهر روند", available_trend_cities, label_visibility="collapsed", key="trend_city")
            trend = history_data[history_data.city_name.eq(trend_city)].copy()
            if country is not None:
                trend = trend[trend.country.eq(country)]
            if selected_pollutant in trend:
                trend["plot_time"] = pd.to_datetime(trend.timestamp, utc=True, errors="coerce")
                trend = trend.dropna(subset=["plot_time",selected_pollutant]).sort_values("plot_time").tail(240)
            if trend.empty or selected_pollutant not in trend:
                st.markdown('<div class="empty">برای این شهر و آلاینده سابقه‌ای موجود نیست.</div>', unsafe_allow_html=True)
            else:
                flag_col = f"quality_{selected_pollutant}"
                color_arg = flag_col if flag_col in trend else None
                fig = px.line(trend,x="plot_time",y=selected_pollutant,color=color_arg,
                              color_discrete_map={"ok":"#43846a","stale":"#b49a6a"} if color_arg else None)
                fig.update_traces(line_width=2.4)
                fig.update_layout(height=320,margin=dict(l=8,r=8,t=12,b=8),paper_bgcolor="white",plot_bgcolor="white",
                                  font_family="Vazirmatn",xaxis_title="",yaxis_title=POLLUTANTS[selected_pollutant][1],
                                  legend_title_text="کیفیت" if color_arg else None,transition_duration=300)
                fig.update_xaxes(showgrid=False)
                fig.update_yaxes(gridcolor="#edf0eb",rangemode="tozero")
                st.plotly_chart(fig,width="stretch",config={"displayModeBar":False})
        else:
            st.markdown('<div class="empty">برای شهرهای انتخاب‌شده تاریخچه‌ای در Gold ثبت نشده است.</div>', unsafe_allow_html=True)
with detail_col:
    with st.container(border=True):
        st.markdown('<div class="panel-title">آخرین اندازه‌گیری‌های تمیز</div><div class="panel-note">زمان مشاهده و زمان ورود جداگانه نمایش داده می‌شوند.</div>', unsafe_allow_html=True)
        if current_data.empty:
            st.markdown('<div class="empty">دادهٔ جاری تمیز وجود ندارد. تاریخچه فقط در بخش روند دیده می‌شود.</div>', unsafe_allow_html=True)
        else:
            detail = current_data.drop_duplicates("city_name",keep="first").copy()
            selected_columns = [c for c in ("timestamp","city_name","country","aqi","pm2_5","pm10","no2","o3","so2","co","source_count","sources") if c in detail]
            detail = detail[selected_columns].rename(columns={"timestamp":"زمان مشاهده","city_name":"شهر","country":"کشور","aqi":"رده EEA",
                "pm2_5":"PM2.5","pm10":"PM10","no2":"NO₂","o3":"O₃","so2":"SO₂","co":"CO","source_count":"منبع PM2.5","sources":"ارائه‌دهندگان"})
            detail["زمان مشاهده"] = detail["زمان مشاهده"].map(format_time)
            if "کشور" in detail:
                detail["کشور"] = detail["کشور"].map(lambda name: f"{COUNTRY_EMOJI.get(name,'')} {COUNTRY_FA.get(name,name)}")
            st.dataframe(detail,width="stretch",hide_index=True,height=330)

with st.expander("کیفیت اجرا و لایه‌های داده"):
    bronze_tab, silver_tab = st.tabs(["گزارش دریافت‌ها · Bronze", "نمونه اندازه‌گیری‌ها · Silver"])
    with bronze_tab:
        if bronze.empty:
            st.caption("هنوز پاسخ یا گزارش API ثبت نشده است.")
        else:
            bronze_view = bronze.rename(columns={"ingested_at":"زمان ورود","city":"شهر","city_name":"شهر","country":"کشور",
                "provider":"ارائه‌دهنده","run_id":"اجرای pipeline","http_status":"HTTP","status":"وضعیت","error":"نوع خطا"}).copy()
            if "زمان ورود" in bronze_view:
                bronze_view["زمان ورود"] = bronze_view["زمان ورود"].map(format_time)
            if "کشور" in bronze_view:
                bronze_view["کشور"] = bronze_view["کشور"].map(lambda name: COUNTRY_FA.get(name,name))
            st.dataframe(bronze_view,width="stretch",hide_index=True,height=260)
    with silver_tab:
        if silver.empty:
            st.caption("هنوز اندازه‌گیری Silver ثبت نشده است.")
        else:
            silver_view = silver.rename(columns={"observed_at":"زمان مشاهده","ingested_at":"زمان ورود","city":"شهر","country":"کشور",
                "provider":"ارائه‌دهنده","pollutant":"آلاینده","value":"مقدار","unit":"واحد","value_type":"نوع مقدار",
                "quality_flag":"کیفیت","run_id":"اجرای pipeline"}).copy()
            for col in ("زمان مشاهده","زمان ورود"):
                if col in silver_view:
                    silver_view[col] = silver_view[col].map(format_time)
            silver_view["کشور"] = silver_view["کشور"].map(lambda name: COUNTRY_FA.get(name,name))
            st.dataframe(silver_view,width="stretch",hide_index=True,height=280)

st.markdown('<div class="footer">نَفَس · مقادیر غلظت بر حسب µg/m³ · داده‌های Open-Meteo مدل‌شده‌اند و اندازه‌گیری ایستگاهی نیستند.</div>', unsafe_allow_html=True)
