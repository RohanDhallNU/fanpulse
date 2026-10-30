import hashlib
import pandas as pd
import streamlit as st
import fanpulse_core as fc

st.set_page_config(page_title="FanPulse", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

INK, TEAL, AMBER, ALARM, SLATE, PAPER = "#172033", "#0E7C7B", "#C98A1E", "#C23B4E", "#5E6A7D", "#F6F8FA"
st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
html, body, [class*="css"], .stMarkdown, .stText, button, input, textarea {{ font-family: 'Manrope', system-ui, sans-serif; }}
.block-container {{ padding-top: 2.2rem; max-width: 1100px; }}
h1, h2, h3 {{ color: {INK}; letter-spacing: -0.01em; }}
.fp-title {{ font-size: 2.6rem; font-weight: 800; color: {INK}; margin: 0; line-height: 1.05; }}
.fp-title span {{ color: {TEAL}; }}
.fp-sub {{ color: {SLATE}; font-size: 1.05rem; max-width: 62ch; margin: .4rem 0 1.4rem; }}
.fp-steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 8px 0 18px; }}
.fp-step {{ border-top: 3px solid {TEAL}; padding: 12px 4px 0; }}
.fp-step b {{ display: block; color: {INK}; font-size: 1rem; margin-bottom: 4px; }}
.fp-step span {{ color: {SLATE}; font-size: .92rem; }}
.fp-fix {{ background: {INK}; color: #fff; border-radius: 14px; padding: 22px 26px; margin: 6px 0 18px; }}
.fp-fix .lbl {{ color: #8FD3CF; font-weight: 700; font-size: .9rem; }}
.fp-fix .ttl {{ font-size: 1.7rem; font-weight: 800; margin: 4px 0 8px; line-height: 1.2; }}
.fp-fix .why {{ color: #D5DBE5; font-size: 1rem; max-width: 70ch; }}
.fp-fix .step {{ margin-top: 12px; background: rgba(255,255,255,.08); border-radius: 10px; padding: 10px 14px; }}
.fp-head {{ font-size: 1.15rem; font-weight: 700; color: {INK}; margin: 0 0 12px; }}
.fp-pain {{ border-left: 4px solid {TEAL}; padding: 6px 0 6px 14px; margin: 0 0 16px; }}
.fp-pain .t {{ font-weight: 800; color: {INK}; font-size: 1.05rem; }}
.fp-pain .m {{ color: {SLATE}; font-size: .9rem; margin: 2px 0 4px; }}
.fp-pain .q {{ color: #3B4658; font-style: italic; font-size: .93rem; }}
.fp-urgent {{ border-left: 4px solid {ALARM}; background: #FFF6F7; border-radius: 0 10px 10px 0; padding: 10px 14px; margin-bottom: 10px; }}
.fp-urgent .o {{ color: {ALARM}; font-weight: 700; font-size: .85rem; }}
.fp-note {{ color: {SLATE}; font-size: .88rem; }}
.fp-err {{ border-left: 4px solid {ALARM}; background: #FFF6F7; border-radius: 0 12px 12px 0; padding: 14px 18px; }}
.fp-err b {{ color: {ALARM}; display: block; font-size: 1.05rem; margin-bottom: 4px; }}
@media (max-width: 760px) {{ .fp-steps {{ grid-template-columns: 1fr; }} .fp-title {{ font-size: 2rem; }} }}
</style>""", unsafe_allow_html=True)


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@st.cache_data(show_spinner=False)
def sample_bytes():
    with open("sample_reviews.csv", "rb") as f:
        return f.read()


@st.cache_resource
def result_cache():
    return {}   # shared across visitors: the same input is only analysed once (saves the free AI quota)


def show_error(msg, fix=""):
    st.markdown(f'<div class="fp-err"><b>{esc(msg)}</b>{esc(fix)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- header
st.markdown('<p class="fp-title">Fan<span>Pulse</span></p>', unsafe_allow_html=True)
st.markdown('<p class="fp-sub">Turn hundreds of app reviews into a ranked list of what is driving customers away, '
            'so you know which problem to fix first.</p>', unsafe_allow_html=True)

# ---------------------------------------------------------------- sidebar inputs
with st.sidebar:
    st.header("Your reviews")
    source = st.radio("Which reviews should FanPulse read?",
                      ["Sample: PrizePicks vs. Underdog (Oct 2026)", "Upload my own CSV"],
                      help="The sample is 180 real App Store reviews of PrizePicks and its competitor Underdog, collected in October 2026.")
    raw, fname = None, None
    if source.startswith("Sample"):
        raw, fname = sample_bytes(), "sample_reviews.csv"
    else:
        up = st.file_uploader("Reviews file (CSV)", type=None,
                              help="One review per row. Needs a review text column and a 1–5 star rating column. "
                                   "Optional: app name, date, title.")
        st.download_button("Download a blank template", "app,date,rating,title,text\n",
                           file_name="fanpulse_template.csv", mime="text/csv")
        if up is not None:
            raw, fname = up.getvalue(), up.name

reviews, headlines, notes, load_error = None, [], [], None
if raw is not None:
    try:
        reviews, headlines, notes = fc.prepare(fc.load_csv(raw, fname))
    except fc.FanPulseError as e:
        load_error = e

with st.sidebar:
    if reviews is not None:
        apps = sorted(reviews["app"].unique().tolist(), key=lambda a: -int((reviews["app"] == a).sum()))
        my_app = st.selectbox("Your app", apps, help="The app you are responsible for. Problems are ranked for this app.")
        others = ["No competitor"] + [a for a in apps if a != my_app]
        comp = st.selectbox("Compare with (optional)", others, index=1 if len(others) > 1 else 0,
                            help="A competitor's reviews show whether a problem is yours or the whole industry's.")
        competitor = None if comp == "No competitor" else comp
        available = int((reviews["app"] == my_app).sum() + ((reviews["app"] == competitor).sum() if competitor else 0))
        top = min(fc.MAX_REVIEWS, available)
        limit = st.slider("How many reviews to read", min_value=min(fc.MIN_REVIEWS, top), max_value=top,
                          value=min(120, top), step=10 if top >= 20 else 1,
                          help="About 1 minute per 100 reviews on the free AI tier.")
        news = st.text_area("Headlines about your brand (optional)", "\n".join(headlines), height=140,
                            help="One per line. FanPulse compares what the brand promises in the news with what customers experience.")
        headlines = [h.strip() for h in news.splitlines() if h.strip()]
        go = st.button("Analyze reviews", type="primary", use_container_width=True)
    else:
        go = False

# ---------------------------------------------------------------- empty / error states
if load_error:
    show_error(load_error.message, " " + load_error.fix)
    st.stop()

key = None
if reviews is not None:
    key = hashlib.sha1(raw + f"|{my_app}|{competitor}|{limit}|{'|'.join(headlines)}".encode()).hexdigest()
cache = result_cache()

if go:
    try:
        api_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        api_key = ""
    if not api_key:
        show_error("The AI connection is not set up on this site.", " The site owner needs to add the AI key in the app settings.")
        st.stop()
    if key not in cache:
        sample = fc.pick_sample(reviews, my_app, competitor, limit)
        bar = st.progress(0.0, text="Starting")
        def progress(done, total, msg):
            bar.progress(min(done / max(total, 1), 1.0) * 0.85, text=msg)
        try:
            cache[key] = fc.analyse(api_key, sample, headlines, my_app, competitor, progress)
        except fc.FanPulseError as e:
            bar.empty()
            show_error(e.message, " " + e.fix)
            st.stop()
        bar.empty()
    st.session_state["key"] = key

res = cache.get(st.session_state.get("key")) if st.session_state.get("key") == key else None

if res is None:
    st.markdown("""<div class="fp-steps">
      <div class="fp-step"><b>1. Choose reviews</b><span>Use the sample, or upload a CSV of your own app’s reviews.</span></div>
      <div class="fp-step"><b>2. Press Analyze reviews</b><span>FanPulse reads every review, sorts it by where customers get stuck, and double-checks unsure calls.</span></div>
      <div class="fp-step"><b>3. Get one clear answer</b><span>The problem to fix first, with real quotes, urgent cases and who should own them.</span></div>
    </div>""", unsafe_allow_html=True)
    if reviews is None:
        st.info("Upload a CSV in the left panel to begin, or switch back to the sample.")
    else:
        st.info(f"{len(reviews)} reviews are ready. Check the settings on the left, then press **Analyze reviews**.")
        with st.expander("Preview the reviews FanPulse will read"):
            st.dataframe(reviews[["app", "date", "rating", "title", "text"]].head(20), use_container_width=True, hide_index=True)
    for n in notes:
        st.caption(n)
    st.stop()

# ---------------------------------------------------------------- results
stats, rep, audit, table = res["stats"], res["report"], res["audit"], res["table"]
mine = stats["mine"]
ff = rep["fix_first"]
st.markdown(f"""<div class="fp-fix"><div class="lbl">Fix this first</div><div class="ttl">{esc(ff.get('title'))}</div>
<div class="why">{esc(ff.get('why'))}</div><div class="step"><b>Next step:</b> {esc(ff.get('first_step'))}</div></div>""",
            unsafe_allow_html=True)
st.markdown(f"**{esc(rep.get('headline'))}**")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Reviews read", f"{stats['tagged']}")
c2.metric(f"{stats['my_app']} rating", f"{mine['avg_rating']} ★",
          delta=(f"{round(mine['avg_rating'] - stats['comp']['avg_rating'], 2)} vs {stats['competitor']}" if stats["comp"] else None))
c3.metric("Negative reviews", f"{mine['pct_negative']}%")
c4.metric("Urgent cases", f"{len(stats['urgent'])}")

tabs = st.tabs(["Top problems", "Urgent cases", "Promise vs. reality", "Vs. competitor", "Next steps", "Checks & downloads"])

with tabs[0]:
    st.markdown('<p class="fp-head">Ranked by how many people it hurts, how badly, and how likely they are to leave</p>', unsafe_allow_html=True)
    for i, p in enumerate(stats["pains"], 1):
        quotes = "".join(f"<div class='q'>“{esc(q)}”</div>" for q in p["quotes"][:2])
        st.markdown(f"<div class='fp-pain'><div class='t'>{i}. {esc(p['label'])}</div>"
                    f"<div class='m'>{p['reviews']} reviews · severity {p['avg_severity']}/5 · {esc(p['stage'])} · owner: {esc(p['owner'])}</div>{quotes}</div>",
                    unsafe_allow_html=True)
    chart = pd.DataFrame({stats["my_app"]: mine["neg_by_stage"]}, index=fc.STAGES)
    if stats["comp"]:
        chart[stats["competitor"]] = pd.Series(stats["comp"]["neg_by_stage"])
    chart.index = [fc.STAGE_LABEL[s] for s in chart.index]
    st.markdown('<p class="fp-head">Where customers get stuck (% of each app’s reviews that are negative)</p>', unsafe_allow_html=True)
    st.bar_chart(chart, horizontal=True, color=[TEAL, AMBER][:len(chart.columns)])

with tabs[1]:
    if not stats["urgent"]:
        st.success("No urgent cases this week.")
    for u in stats["urgent"]:
        st.markdown(f"<div class='fp-urgent'><div class='o'>Send to: {esc(u['owner'])} · severity {u['severity']}/5</div>"
                    f"<b>{esc(u['issue_summary'])}</b><div class='fp-note'>“{esc(u['evidence'])}” ({u['rating']}★ {esc(u['date'])})</div></div>",
                    unsafe_allow_html=True)

with tabs[2]:
    pv = rep.get("promise_vs_reality")
    if not pv:
        st.info("Add a few headlines about your brand in the left panel to see how the brand’s promises compare with customer experience.")
    else:
        a, b = st.columns(2)
        a.markdown(f"**What the brand promises**\n\n{pv.get('promise', '')}")
        b.markdown(f"**What customers experience**\n\n{pv.get('reality', '')}")
        st.markdown(f"**The gap:** {pv.get('gap', '')}\n\n**Recommendation:** {pv.get('recommendation', '')}")

with tabs[3]:
    cv = rep.get("competitor_view")
    if not cv:
        st.info("Pick a competitor in the left panel to see where you are winning and losing.")
    else:
        st.markdown(cv.get("summary", ""))
        a, b = st.columns(2)
        a.markdown("**Where you are losing**\n" + "".join(f"\n- {x}" for x in cv.get("losing_on", []) if x))
        b.markdown("**Where you are winning**\n" + "".join(f"\n- {x}" for x in cv.get("winning_on", []) if x))

with tabs[4]:
    if rep.get("ab_tests"):
        st.markdown('<p class="fp-head">Tests to run next week</p>', unsafe_allow_html=True)
        for t in rep["ab_tests"]:
            st.markdown(f"**{t.get('name', '')}**  \n{t.get('hypothesis', '')}  \n*Measure:* {t.get('metric', '')}")
    sr = rep.get("support_reply")
    if sr:
        st.markdown('<p class="fp-head">Ready-to-send support reply</p>', unsafe_allow_html=True)
        st.markdown(f"*For: {sr.get('issue', '')}*")
        st.code(sr.get("reply", ""), language=None)

with tabs[5]:
    st.markdown(f"- **{stats['tagged']} of {stats['total']}** reviews were tagged by the AI.")
    if res["failed"]:
        st.markdown(f"- {len(res['failed'])} reviews could not be read by the AI ({res['failed'][0][1]}) and are marked for manual review.")
    st.markdown(f"- **Second opinion:** {audit['flagged']} unsure tags were re-checked by a second model; "
                f"{audit['override']} were corrected, {audit['agree']} confirmed.")
    for ex in audit["examples"][:5]:
        st.caption(f"{ex['id']}: changed from “{ex['before']}” to “{ex['after']}”. {ex['reason']}")
    for n in notes + res["notes"]:
        st.markdown(f"- {n}")
    st.markdown(f"- Finished in {res['seconds']} seconds.")
    out = table[["id", "app", "date", "rating", "title", "text", "funnel_stage", "theme", "sentiment", "severity",
                 "churn_risk", "urgent", "owner", "issue_summary", "evidence"]]
    d1, d2 = st.columns(2)
    d1.download_button("Download tagged reviews (CSV)", out.to_csv(index=False), "fanpulse_tagged_reviews.csv", "text/csv",
                       use_container_width=True)
    d2.download_button("Download summary (Markdown)", fc.report_markdown(stats, rep, audit), "fanpulse_summary.md",
                       "text/markdown", use_container_width=True)
