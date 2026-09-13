from __future__ import annotations

import json
import os
import re
import traceback
import zipfile
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, List, Iterator, Tuple

import streamlit as st
import streamlit.components.v1 as components

# -----------------------------------------------------------------------
# Import the existing, untouched LangGraph application.
# All research / planning / writing / image logic lives in bwa_backend.py.
# This frontend only orchestrates *how* that app is called and displayed.
# -----------------------------------------------------------------------
from bwa_backend import app


# =========================================================================
# 1) LOW-LEVEL HELPERS (kept / lightly adapted from the original frontend)
# =========================================================================

def safe_slug(title: str) -> str:
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9 _-]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s or "blog"


def bundle_zip(md_text: str, md_filename: str, images_dir: Path) -> bytes:
    """Zip the markdown file together with any generated images."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(md_filename, md_text.encode("utf-8"))
        if images_dir.exists() and images_dir.is_dir():
            for p in images_dir.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=str(p))
    return buf.getvalue()


def try_stream(graph_app, inputs: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    """
    Stream graph progress if the installed LangGraph version supports it,
    otherwise fall back to a single blocking invoke(). Behaviour/order of
    the underlying graph is completely untouched.
    """
    try:
        for step in graph_app.stream(inputs, stream_mode="updates"):
            yield ("updates", step)
        out = graph_app.invoke(inputs)
        yield ("final", out)
        return
    except Exception:
        pass

    try:
        for step in graph_app.stream(inputs, stream_mode="values"):
            yield ("values", step)
        out = graph_app.invoke(inputs)
        yield ("final", out)
        return
    except Exception:
        pass

    out = graph_app.invoke(inputs)
    yield ("final", out)


def extract_latest_state(current_state: Dict[str, Any], step_payload: Any) -> Dict[str, Any]:
    if isinstance(step_payload, dict):
        if len(step_payload) == 1 and isinstance(next(iter(step_payload.values())), dict):
            inner = next(iter(step_payload.values()))
            current_state.update(inner)
        else:
            current_state.update(step_payload)
    return current_state


def as_dict(obj: Any) -> Dict[str, Any]:
    """Normalize a pydantic model / dict / other into a plain dict."""
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return {}


# -----------------------------------------------------------------------
# Markdown renderer that supports local (relative) images produced by the
# backend's image pipeline (images/<file>.png referenced inline in the md).
# -----------------------------------------------------------------------
_MD_IMG_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)")
_CAPTION_LINE_RE = re.compile(r"^\*(?P<cap>.+)\*$")


def _resolve_image_path(src: str) -> Path:
    src = src.strip().lstrip("./")
    return Path(src).resolve()


def render_markdown_with_local_images(md: str):
    matches = list(_MD_IMG_RE.finditer(md))
    if not matches:
        st.markdown(md, unsafe_allow_html=False)
        return

    parts: List[Tuple[str, str]] = []
    last = 0
    for m in matches:
        before = md[last: m.start()]
        if before:
            parts.append(("md", before))
        alt = (m.group("alt") or "").strip()
        src = (m.group("src") or "").strip()
        parts.append(("img", f"{alt}|||{src}"))
        last = m.end()

    tail = md[last:]
    if tail:
        parts.append(("md", tail))

    i = 0
    while i < len(parts):
        kind, payload = parts[i]

        if kind == "md":
            st.markdown(payload, unsafe_allow_html=False)
            i += 1
            continue

        alt, src = payload.split("|||", 1)
        caption = None
        if i + 1 < len(parts) and parts[i + 1][0] == "md":
            nxt = parts[i + 1][1].lstrip()
            if nxt.strip():
                first_line = nxt.splitlines()[0].strip()
                mcap = _CAPTION_LINE_RE.match(first_line)
                if mcap:
                    caption = mcap.group("cap").strip()
                    rest = "\n".join(nxt.splitlines()[1:])
                    parts[i + 1] = ("md", rest)

        if src.startswith("http://") or src.startswith("https://"):
            st.image(src, caption=caption or (alt or None), use_container_width=True)
        else:
            img_path = _resolve_image_path(src)
            if img_path.exists():
                st.image(str(img_path), caption=caption or (alt or None), use_container_width=True)
            else:
                st.warning(f"Image not found: `{src}`")
        i += 1


def md_to_html(md_text: str, title: str) -> str:
    """
    Best-effort Markdown -> standalone HTML conversion for the
    'Download HTML' action. Uses the `markdown` package if installed,
    otherwise falls back to a minimal, safe converter so the button
    never silently fails.
    """
    body_html: str
    try:
        import markdown as _markdown  # optional dependency, see requirements note
        body_html = _markdown.markdown(
            md_text, extensions=["tables", "fenced_code", "sane_lists"]
        )
    except Exception:
        # Minimal fallback: escape HTML, preserve line breaks and headings.
        import html as _html
        escaped = _html.escape(md_text)
        lines = []
        for line in escaped.splitlines():
            if line.startswith("### "):
                lines.append(f"<h3>{line[4:]}</h3>")
            elif line.startswith("## "):
                lines.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("# "):
                lines.append(f"<h1>{line[2:]}</h1>")
            elif line.strip() == "":
                lines.append("<br>")
            else:
                lines.append(f"<p>{line}</p>")
        body_html = "\n".join(lines)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Inter, Arial, sans-serif;
          max-width: 800px; margin: 40px auto; padding: 0 20px;
          color: #1e293b; line-height: 1.7; }}
  h1, h2, h3 {{ color: #0f172a; }}
  img {{ max-width: 100%; border-radius: 8px; }}
  code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }}
  pre {{ background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 8px; overflow-x: auto; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# -----------------------------------------------------------------------
# Past blogs (files saved by the backend as *.md in the working directory)
# -----------------------------------------------------------------------
def list_past_blogs() -> List[Path]:
    cwd = Path(".")
    files = [p for p in cwd.glob("*.md") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def read_md_file(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def extract_title_from_md(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


# =========================================================================
# 2) PAGE CONFIG + STYLING
# =========================================================================

st.set_page_config(
    page_title="BlogCraft AI — Research. Write. Publish.",
    page_icon="✨",
    layout="wide",
    initial_sidebar_state="expanded",
)


def inject_custom_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

        :root {
            --bc-bg: #f8fafc;
            --bc-surface: #ffffff;
            --bc-border: #e2e8f0;
            --bc-text: #0f172a;
            --bc-muted: #64748b;
            --bc-primary: #4f46e5;
            --bc-primary-dark: #4338ca;
            --bc-accent: #7c3aed;
            --bc-success: #16a34a;
            --bc-danger: #dc2626;
        }

        .main .block-container { padding-top: 1.2rem; max-width: 1200px; }
        #MainMenu, footer { visibility: hidden; }

        /* ---------- Header ---------- */
        .bc-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 22px; background: var(--bc-surface);
            border: 1px solid var(--bc-border); border-radius: 14px;
            margin-bottom: 18px;
        }
        .bc-brand { display: flex; align-items: center; gap: 12px; }
        .bc-logo {
            width: 40px; height: 40px; border-radius: 10px;
            background: linear-gradient(135deg, var(--bc-primary), var(--bc-accent));
            display: flex; align-items: center; justify-content: center;
            font-size: 20px; color: white;
        }
        .bc-brand-name { font-weight: 800; font-size: 1.15rem; color: var(--bc-text); line-height: 1.1; }
        .bc-brand-tag { font-size: 0.8rem; color: var(--bc-muted); }
        .bc-status {
            display: flex; align-items: center; gap: 8px;
            font-size: 0.82rem; font-weight: 600; color: var(--bc-success);
            background: #f0fdf4; border: 1px solid #bbf7d0;
            padding: 6px 12px; border-radius: 999px;
        }
        .bc-status .dot {
            width: 8px; height: 8px; border-radius: 50%; background: var(--bc-success);
            box-shadow: 0 0 0 3px rgba(22,163,74,0.15);
        }

        /* ---------- Hero ---------- */
        .bc-hero {
            padding: 30px 32px; border-radius: 16px; margin-bottom: 20px;
            background: linear-gradient(120deg, #eef2ff 0%, #f5f3ff 55%, #fdf4ff 100%);
            border: 1px solid #e9e5ff;
        }
        .bc-hero h1 { font-size: 1.9rem; font-weight: 800; color: var(--bc-text); margin: 0 0 6px 0; }
        .bc-hero p { font-size: 1.02rem; color: var(--bc-muted); margin: 0; }

        /* ---------- Cards ---------- */
        .bc-card {
            background: var(--bc-surface); border: 1px solid var(--bc-border);
            border-radius: 14px; padding: 20px 22px; margin-bottom: 16px;
        }
        .bc-card h3 { margin-top: 0; font-size: 1.02rem; font-weight: 700; color: var(--bc-text); }
        .bc-section-label {
            font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em;
            text-transform: uppercase; color: var(--bc-muted); margin-bottom: 6px;
        }

        /* ---------- Buttons ---------- */
        .stButton > button {
            border-radius: 10px; font-weight: 600; border: 1px solid var(--bc-border);
        }
        .stButton > button[kind="primary"] {
            background: linear-gradient(120deg, var(--bc-primary), var(--bc-accent));
            border: none; color: white; font-weight: 700;
            box-shadow: 0 4px 14px rgba(79,70,229,0.28);
        }
        .stButton > button[kind="primary"]:hover { opacity: 0.94; }

        /* ---------- Example topic chips ---------- */
        .bc-chip {
            display: inline-block; padding: 6px 14px; margin: 4px 6px 4px 0;
            border-radius: 999px; background: #f1f5f9; border: 1px solid var(--bc-border);
            font-size: 0.85rem; color: var(--bc-text);
        }

        /* ---------- Article reading layout ---------- */
        .bc-article {
            background: var(--bc-surface); border: 1px solid var(--bc-border);
            border-radius: 16px; padding: 40px 48px; margin-top: 6px;
        }
        .bc-article h1 { font-size: 2rem; font-weight: 800; color: var(--bc-text); margin-bottom: 4px; }
        .bc-article h2 { font-size: 1.35rem; font-weight: 700; color: var(--bc-text); margin-top: 1.6em; }
        .bc-article h3 { font-size: 1.1rem; font-weight: 700; color: var(--bc-text); }
        .bc-article p, .bc-article li { font-size: 1.02rem; line-height: 1.75; color: #1e293b; }
        .bc-article img { border-radius: 10px; }

        .bc-meta-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 6px 0 18px 0; }
        .bc-pill {
            font-size: 0.75rem; font-weight: 600; padding: 4px 11px; border-radius: 999px;
            background: #eef2ff; color: var(--bc-primary-dark); border: 1px solid #e0e7ff;
        }

        /* ---------- Empty state ---------- */
        .bc-empty {
            text-align: center; padding: 60px 20px; border: 1.5px dashed var(--bc-border);
            border-radius: 16px; background: var(--bc-surface);
        }
        .bc-empty h2 { color: var(--bc-text); font-weight: 700; margin-bottom: 6px; }
        .bc-empty p { color: var(--bc-muted); }

        /* ---------- Footer ---------- */
        .bc-footer { text-align: center; color: var(--bc-muted); font-size: 0.8rem; margin-top: 34px; padding: 16px 0; }

        /* ---------- Sidebar (hardened against dark-theme text/bg clashes) ---------- */
        section[data-testid="stSidebar"] {
            background: #ffffff !important;
            border-right: 1px solid var(--bc-border);
        }
        /* Force readable dark text on our white sidebar regardless of the
           user's light/dark Streamlit theme preference. */
        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3,
        section[data-testid="stSidebar"] h4,
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] li,
        section[data-testid="stSidebar"] div[data-testid="stMarkdownContainer"] {
            color: var(--bc-text) !important;
        }
        section[data-testid="stSidebar"] small,
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
            color: var(--bc-muted) !important;
        }
        /* Buttons and select boxes have their own backgrounds in dark theme —
           force a light control surface so the forced-dark text stays legible. */
        section[data-testid="stSidebar"] button {
            background: #f1f5f9 !important;
            color: var(--bc-text) !important;
            border: 1px solid var(--bc-border) !important;
        }
        section[data-testid="stSidebar"] div[data-baseweb="select"] > div {
            background-color: #ffffff !important;
            color: var(--bc-text) !important;
            border: 1px solid var(--bc-border) !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_custom_css()


# =========================================================================
# 3) SERVICE STATUS
# =========================================================================

def check_service_status() -> Dict[str, bool]:
    """Reports whether required env vars are configured — never displays values."""
    return {
        "OpenAI": bool(os.getenv("OPENAI_API_KEY")),
        "Tavily (research)": bool(os.getenv("TAVILY_API_KEY")),
        "Google (images)": bool(os.getenv("GOOGLE_API_KEY")),
    }


# =========================================================================
# 4) HEADER / HERO
# =========================================================================

def render_header():
    st.markdown(
        """
        <div class="bc-header">
            <div class="bc-brand">
                <div class="bc-logo">✨</div>
                <div>
                    <div class="bc-brand-name">BlogCraft AI</div>
                    <div class="bc-brand-tag">Research. Write. Publish.</div>
                </div>
            </div>
            <div class="bc-status"><span class="dot"></span> AI Agent Online</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero():
    st.markdown(
        """
        <div class="bc-hero">
            <h1>Turn Ideas Into High-Quality Blogs</h1>
            <p>Research-backed AI writing powered by intelligent agents.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================================================================
# 5) SIDEBAR
# =========================================================================

def render_sidebar(status: Dict[str, bool]):
    with st.sidebar:
        st.markdown("### 🖋️ AI Blog Writer")

        st.markdown("#### Settings")
        for name, ok in status.items():
            icon = "✅" if ok else "⚠️"
            label = "connected" if ok else "not configured"
            st.markdown(f"{icon} **{name}** — {label}")
        st.caption(
            "Research and image generation are decided automatically by the "
            "agent based on your topic — they only run when the relevant "
            "service above is connected."
        )

        st.divider()
        st.markdown("#### About")
        st.caption(
            "An AI-powered blog writing agent that researches topics and "
            "generates structured, high-quality content."
        )

        st.divider()
        st.markdown("#### Workflow")
        steps = ["Topic", "Research", "Planning", "Writing", "Refinement", "Final Blog"]
        st.markdown(
            "".join(f"<div style='padding:3px 0;color:#334155;'>{i}. {s}</div>"
                     for i, s in enumerate(steps, start=1)),
            unsafe_allow_html=True,
        )

        st.divider()
        st.markdown("#### Past Blogs")
        past_files = list_past_blogs()
        if not past_files:
            st.caption("No saved blogs yet.")
        else:
            options, file_by_label = [], {}
            for p in past_files[:50]:
                try:
                    title = extract_title_from_md(read_md_file(p), p.stem)
                except Exception:
                    title = p.stem
                label = f"{title} · {p.name}"
                options.append(label)
                file_by_label[label] = p

            selected_label = st.selectbox("Load a previous blog", options, label_visibility="collapsed")
            if st.button("📂 Load selected blog", use_container_width=True):
                chosen = file_by_label.get(selected_label)
                if chosen:
                    md_text = read_md_file(chosen)
                    st.session_state["last_out"] = {
                        "plan": None,
                        "evidence": [],
                        "image_specs": [],
                        "final": md_text,
                    }
                    st.session_state["edited_markdown"] = md_text
                    st.session_state["edit_mode"] = False
                    st.rerun()


# =========================================================================
# 6) CONFIGURATION PANEL
# =========================================================================

EXAMPLE_TOPICS = [
    "State of Multimodal LLMs in 2026",
    "Open Source LLMs in 2026",
    "Future of AI Agents",
    "AI in Education",
]

TONE_OPTIONS = ["Let AI decide", "Professional", "Conversational", "Enthusiastic", "Analytical"]
AUDIENCE_OPTIONS = ["Let AI decide", "Beginners", "Developers", "Business leaders", "General readers"]
LENGTH_OPTIONS = ["Let AI decide", "Short (~600-900 words)", "Medium (~1200-1800 words)", "Long (~2000+ words)"]


def render_config_panel() -> Dict[str, Any]:
    st.markdown('<div class="bc-card">', unsafe_allow_html=True)
    st.markdown("### 📝 Blog Configuration")

    topic = st.text_area(
        "Blog topic",
        key="topic_input",
        placeholder="e.g. The Future of AI Agents in Enterprise Software",
        height=100,
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        tone = st.selectbox("Tone", TONE_OPTIONS, key="tone_input")
    with col2:
        audience = st.selectbox("Target audience", AUDIENCE_OPTIONS, key="audience_input")
    with col3:
        length_choice = st.selectbox("Desired length", LENGTH_OPTIONS, key="length_input")

    as_of = st.date_input("As-of date (used for freshness of research)", value=date.today(), key="as_of_input")

    st.caption(
        "ℹ️ Tone, audience and length are passed to the agent as writing "
        "guidance. Whether web research or images are used is decided "
        "automatically by the agent for each topic."
    )
    st.markdown("</div>", unsafe_allow_html=True)

    return {"topic": topic, "tone": tone, "audience": audience, "length_choice": length_choice, "as_of": as_of}


def build_effective_topic(topic: str, tone: str, audience: str, length_choice: str) -> str:
    """
    The backend graph only accepts a single 'topic' string plus 'as_of'.
    Rather than inventing backend fields that don't exist, we fold any
    non-default writing preferences into the topic text itself as clear
    guidance the planning/writing prompts will read as context.
    """
    prefs = []
    if tone != "Let AI decide":
        prefs.append(f"tone: {tone.lower()}")
    if audience != "Let AI decide":
        prefs.append(f"target audience: {audience.lower()}")
    if length_choice != "Let AI decide":
        prefs.append(f"desired length: {length_choice.split('(')[-1].rstrip(')')}")

    if not prefs:
        return topic.strip()

    return f"{topic.strip()}\n\n(Writing preferences — {', '.join(prefs)}.)"


def render_example_topics():
    st.markdown('<div class="bc-card">', unsafe_allow_html=True)
    st.markdown('<div class="bc-section-label">Need inspiration? Try one of these</div>', unsafe_allow_html=True)
    cols = st.columns(4)
    for col, ex in zip(cols, EXAMPLE_TOPICS):
        with col:
            if st.button(ex, key=f"example_{safe_slug(ex)}", use_container_width=True):
                st.session_state["topic_input"] = ex
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


# =========================================================================
# 7) GENERATION (progress + graph invocation)
# =========================================================================

NODE_PHASE_MESSAGES = {
    "router": "🔎 Analyzing topic...",
    "research": "🌐 Researching latest information...",
    "orchestrator": "🧩 Creating article structure...",
    "worker": "✍️ Writing the blog...",
    "merge_content": "🧵 Merging sections...",
    "decide_images": "🖼️ Planning visuals...",
    "generate_and_place_images": "🎨 Generating images...",
    "reducer": "💅 Polishing final content...",
}


def generate_blog(topic: str, as_of: date) -> Dict[str, Any]:
    """
    Calls the existing LangGraph app exactly as before, streaming progress
    into the UI. Raises on failure so the caller can render a friendly
    error instead of a raw traceback.
    """
    inputs: Dict[str, Any] = {
        "topic": topic,
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": as_of.isoformat(),
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "md_with_placeholders": "",
        "image_specs": [],
        "final": "",
    }

    status_box = st.status("AI agent is working on your blog...", expanded=True)
    seen_nodes: List[str] = []
    current_state: Dict[str, Any] = {}
    final_out: Optional[Dict[str, Any]] = None

    for kind, payload in try_stream(app, inputs):
        if kind in ("updates", "values"):
            node_name = None
            if isinstance(payload, dict) and len(payload) == 1 and isinstance(next(iter(payload.values())), dict):
                node_name = next(iter(payload.keys()))

            if node_name and node_name not in seen_nodes:
                seen_nodes.append(node_name)
                phase = NODE_PHASE_MESSAGES.get(node_name, f"Working on `{node_name}`...")
                status_box.write(phase)

            current_state = extract_latest_state(current_state, payload)

        elif kind == "final":
            final_out = payload

    if final_out is None:
        raise RuntimeError("The agent did not return a final result.")

    status_box.update(label="✅ Blog ready!", state="complete", expanded=False)
    return final_out


# =========================================================================
# 8) RESULTS RENDERING
# =========================================================================

def extract_blog_title(out: Dict[str, Any], fallback_topic: str) -> str:
    plan = as_dict(out.get("plan"))
    if plan.get("blog_title"):
        return plan["blog_title"]
    final_md = out.get("final") or ""
    return extract_title_from_md(final_md, fallback_topic or "Untitled Blog")


def render_meta_pills(out: Dict[str, Any]):
    plan = as_dict(out.get("plan"))
    evidence = out.get("evidence") or []
    image_specs = out.get("image_specs") or []

    pills = []
    if plan.get("audience"):
        pills.append(f"👤 {plan['audience']}")
    if plan.get("tone"):
        pills.append(f"🎭 {plan['tone']}")
    if plan.get("blog_kind"):
        pills.append(f"🏷️ {plan['blog_kind'].replace('_', ' ').title()}")
    pills.append(f"📚 {len(evidence)} source{'s' if len(evidence) != 1 else ''}")
    pills.append(f"🖼️ {len(image_specs)} image{'s' if len(image_specs) != 1 else ''}")

    st.markdown(
        '<div class="bc-meta-row">' + "".join(f'<span class="bc-pill">{p}</span>' for p in pills) + "</div>",
        unsafe_allow_html=True,
    )


def render_sources(out: Dict[str, Any]):
    evidence = out.get("evidence") or []
    if not evidence:
        return
    with st.expander(f"📚 Sources & references ({len(evidence)})"):
        for e in evidence:
            e = as_dict(e)
            title = e.get("title") or e.get("url") or "Source"
            url = e.get("url") or "#"
            date_str = e.get("published_at")
            suffix = f" · {date_str}" if date_str else ""
            st.markdown(f"- [{title}]({url}){suffix}")


def render_copy_button(text: str, key: str):
    """Real clipboard copy using a small embedded HTML/JS component."""
    safe_text = json.dumps(text)
    components.html(
        f"""
        <div>
        <button id="copy-btn-{key}" style="
            background: linear-gradient(120deg, #4f46e5, #7c3aed);
            color: white; border: none; padding: 8px 16px; border-radius: 8px;
            font-family: Inter, sans-serif; font-weight: 600; font-size: 0.85rem;
            cursor: pointer; width: 100%;">
            📋 Copy blog to clipboard
        </button>
        <script>
        const btn = document.getElementById("copy-btn-{key}");
        btn.addEventListener("click", () => {{
            navigator.clipboard.writeText({safe_text}).then(() => {{
                btn.innerText = "✅ Copied!";
                setTimeout(() => btn.innerText = "📋 Copy blog to clipboard", 1800);
            }});
        }});
        </script>
        </div>
        """,
        height=46,
    )


def render_download_buttons(final_md: str, blog_title: str):
    slug = safe_slug(blog_title)
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.download_button(
            "⬇️ Markdown", data=final_md.encode("utf-8"),
            file_name=f"{slug}.md", mime="text/markdown", use_container_width=True,
        )
    with col2:
        st.download_button(
            "⬇️ TXT", data=final_md.encode("utf-8"),
            file_name=f"{slug}.txt", mime="text/plain", use_container_width=True,
        )
    with col3:
        html_doc = md_to_html(final_md, blog_title)
        st.download_button(
            "⬇️ HTML", data=html_doc.encode("utf-8"),
            file_name=f"{slug}.html", mime="text/html", use_container_width=True,
        )
    with col4:
        images_dir = Path("images")
        bundle = bundle_zip(final_md, f"{slug}.md", images_dir)
        st.download_button(
            "📦 Bundle (.zip)", data=bundle,
            file_name=f"{slug}_bundle.zip", mime="application/zip", use_container_width=True,
        )


def render_action_bar(out: Dict[str, Any], blog_title: str, config: Dict[str, Any]):
    final_md = st.session_state.get("edited_markdown", out.get("final") or "")

    action_col1, action_col2 = st.columns([1, 1])
    with action_col1:
        render_copy_button(final_md, key="main")
    with action_col2:
        btn_cols = st.columns(2)
        with btn_cols[0]:
            if st.button("🔄 Regenerate", use_container_width=True):
                st.session_state["trigger_generation"] = True
                st.rerun()
        with btn_cols[1]:
            edit_label = "✅ Done editing" if st.session_state.get("edit_mode") else "📝 Edit"
            if st.button(edit_label, use_container_width=True):
                st.session_state["edit_mode"] = not st.session_state.get("edit_mode", False)
                st.rerun()

    st.write("")
    render_download_buttons(final_md, blog_title)


def render_blog(out: Dict[str, Any], fallback_topic: str, config: Dict[str, Any]):
    blog_title = extract_blog_title(out, fallback_topic)
    final_md = out.get("final") or ""

    if "edited_markdown" not in st.session_state or st.session_state.get("_edited_for") != blog_title:
        st.session_state["edited_markdown"] = final_md
        st.session_state["_edited_for"] = blog_title

    st.markdown("## 📄 Your Blog")
    render_meta_pills(out)
    render_action_bar(out, blog_title, config)

    st.write("")

    if st.session_state.get("edit_mode"):
        st.markdown('<div class="bc-card">', unsafe_allow_html=True)
        edited = st.text_area(
            "Edit markdown", value=st.session_state["edited_markdown"], height=500,
            label_visibility="collapsed",
        )
        if st.button("💾 Save changes"):
            st.session_state["edited_markdown"] = edited
            st.success("Changes saved.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.markdown('<div class="bc-article">', unsafe_allow_html=True)
        render_markdown_with_local_images(st.session_state.get("edited_markdown", final_md))
        st.markdown("</div>", unsafe_allow_html=True)

    render_sources(out)

    with st.expander("🧩 Outline used by the agent"):
        plan = as_dict(out.get("plan"))
        tasks = plan.get("tasks") or []
        if not tasks:
            st.caption("No outline metadata available for this blog.")
        else:
            for t in sorted(tasks, key=lambda x: x.get("id", 0)):
                st.markdown(f"**{t.get('id')}. {t.get('title')}** — {t.get('target_words')} words")
                for b in t.get("bullets", []):
                    st.markdown(f"- {b}")


# =========================================================================
# 9) EMPTY STATE / ERRORS / FOOTER
# =========================================================================

def render_empty_state():
    st.markdown(
        """
        <div class="bc-empty">
            <h2>Your next great article starts here.</h2>
            <p>Enter a topic above and let AI research, structure, and write your blog.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_error(exc: Exception):
    st.error("Something went wrong while generating your blog. Please try again.")
    with st.expander("Technical details (for debugging)"):
        st.code("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), language="text")


def render_footer():
    st.markdown(
        '<div class="bc-footer">Built with Streamlit • LangGraph • LangChain • AI</div>',
        unsafe_allow_html=True,
    )


# =========================================================================
# 10) MAIN APP FLOW
# =========================================================================

def main():
    # ---- session defaults ----
    st.session_state.setdefault("last_out", None)
    st.session_state.setdefault("last_inputs", None)
    st.session_state.setdefault("edit_mode", False)
    st.session_state.setdefault("trigger_generation", False)

    status = check_service_status()

    render_header()
    render_sidebar(status)
    render_hero()

    config = render_config_panel()
    render_example_topics()

    generate_clicked = st.button("✨ Generate Blog", type="primary", use_container_width=True)

    should_generate = generate_clicked or st.session_state.pop("trigger_generation", False)

    if should_generate:
        topic_raw = (config["topic"] or "").strip()
        if not topic_raw:
            st.warning("Please enter a topic before generating your blog.")
        elif not status["OpenAI"]:
            st.error(
                "OpenAI is not configured. Please set the `OPENAI_API_KEY` "
                "environment variable and restart the app."
            )
        else:
            effective_topic = build_effective_topic(
                topic_raw, config["tone"], config["audience"], config["length_choice"]
            )
            try:
                out = generate_blog(effective_topic, config["as_of"])
                st.session_state["last_out"] = out
                st.session_state["last_inputs"] = {
                    "topic": effective_topic,
                    "as_of": config["as_of"],
                    "display_topic": topic_raw,
                }
                st.session_state["edit_mode"] = False
                st.session_state.pop("edited_markdown", None)
            except Exception as exc:
                render_error(exc)

    st.write("")

    out = st.session_state.get("last_out")
    if out:
        fallback_topic = (st.session_state.get("last_inputs") or {}).get("display_topic", "")
        render_blog(out, fallback_topic, config)
    else:
        render_empty_state()

    render_footer()


if __name__ == "__main__":
    main()