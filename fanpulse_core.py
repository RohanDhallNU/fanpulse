"""FanPulse core logic: validation, AI tagging, second opinion, stats, report.
Kept separate from the Streamlit UI so it can be tested on its own."""
import io, json, re, time
import pandas as pd
import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TAGGER_MODEL = "gemini-3.5-flash-lite"
CRITIC_MODEL = "gemini-3.1-flash-lite"
REPORTER_MODEL = "gemini-3.8-flash"
FALLBACK_MODEL = "gemini-3.5-flash-lite"
BATCH_SIZE = 15            # reviews per AI call
CALL_GAP_SECONDS = 4.5     # stays under the free tier's 15 requests/minute
MIN_REVIEWS = 10
MAX_REVIEWS = 200
CONFIDENCE_THRESHOLD = 0.7

STAGES = ["onboarding", "promos", "payments", "gameplay", "support_account", "general"]
THEMES = ["withdrawal_delay", "deposit_issue", "id_verification", "account_restricted", "bonus_not_honored",
          "promo_terms", "payout_rules", "app_bugs", "lines_and_odds", "customer_support",
          "responsible_gaming", "positive_experience", "other"]
SENTIMENTS = ["positive", "neutral", "negative", "mixed"]
CHURN = ["low", "medium", "high"]

STAGE_LABEL = {"onboarding": "Sign-up & login", "promos": "Promos & bonuses", "payments": "Deposits & withdrawals",
               "gameplay": "Using the product", "support_account": "Support & account access", "general": "General"}
THEME_LABEL = {
    "withdrawal_delay": "Withdrawals slow or blocked", "deposit_issue": "Deposits failing or missing",
    "id_verification": "ID verification problems", "account_restricted": "Accounts locked or restricted",
    "bonus_not_honored": "Bonuses not honored", "promo_terms": "Confusing promo terms",
    "payout_rules": "Unclear payout rules", "app_bugs": "Crashes and bugs", "lines_and_odds": "Lines and odds feel unfair",
    "customer_support": "Unhelpful customer support", "responsible_gaming": "Problem-gambling signals",
    "positive_experience": "Praise", "other": "Other"}
OWNER = {"withdrawal_delay": "Payments team", "deposit_issue": "Payments team", "payout_rules": "Product team",
         "id_verification": "Onboarding / KYC team", "account_restricted": "Trust & Safety",
         "bonus_not_honored": "Promotions team", "promo_terms": "Promotions team", "app_bugs": "Engineering",
         "lines_and_odds": "Product team", "customer_support": "Support operations",
         "responsible_gaming": "Responsible Gaming team", "positive_experience": "Marketing", "other": "Weekly review"}


class FanPulseError(Exception):
    """A problem the user can fix. The message is shown as-is, so it must be plain English."""
    def __init__(self, message, fix=""):
        super().__init__(message)
        self.message, self.fix = message, fix


# ---------------------------------------------------------------- input checks
TEXT_COLS = ["text", "review", "content", "body", "comment", "review_text"]
RATING_COLS = ["rating", "stars", "score", "star_rating"]
APP_COLS = ["app", "app_name", "product", "brand"]
DATE_COLS = ["date", "review_date", "created_at", "updated"]
TITLE_COLS = ["title", "review_title", "headline"]
ID_COLS = ["record_id", "id", "review_id"]


def _pick(cols, options):
    for o in options:
        if o in cols:
            return o
    return None


def load_csv(file_bytes, filename="upload.csv"):
    if not filename.lower().endswith(".csv"):
        raise FanPulseError(f"“{filename}” is not a CSV file.",
                            "In Excel or Google Sheets choose File → Save as / Download → CSV, then upload that file.")
    try:
        df = pd.read_csv(io.BytesIO(file_bytes), encoding="utf-8-sig", dtype=str, keep_default_na=False)
    except UnicodeDecodeError:
        df = pd.read_csv(io.BytesIO(file_bytes), encoding="latin-1", dtype=str, keep_default_na=False)
    except Exception:
        raise FanPulseError("This file could not be read as a table.",
                            "Make sure it is a comma-separated CSV with one review per row and a header row.")
    if df.empty:
        raise FanPulseError("The file is empty.", "Upload a CSV that has at least 10 reviews in it.")
    return df


def prepare(df, default_app="My app"):
    """Return (reviews_df, headlines list, notes list). Raises FanPulseError on unusable data."""
    df = df.copy()
    df.columns = [str(c).replace("\ufeff", "").strip().lower() for c in df.columns]
    cols = list(df.columns)
    tcol, rcol = _pick(cols, TEXT_COLS), _pick(cols, RATING_COLS)
    if not tcol:
        raise FanPulseError("FanPulse could not find a column with the review text.",
                            f"Name that column “text” or “review”. Columns found: {', '.join(cols[:12])}.")
    if not rcol and "source_type" not in cols:
        raise FanPulseError("FanPulse could not find a star-rating column.",
                            f"Name it “rating” (1 to 5). Columns found: {', '.join(cols[:12])}.")
    acol, dcol, ticol, icol = _pick(cols, APP_COLS), _pick(cols, DATE_COLS), _pick(cols, TITLE_COLS), _pick(cols, ID_COLS)

    headlines = []
    if "source_type" in cols:
        news = df[df["source_type"].str.lower() == "news"]
        for _, r in news.iterrows():
            h = str(r.get(ticol, "") or r.get(tcol, "")).strip()
            if h:
                headlines.append(h)
        df = df[df["source_type"].str.lower() != "news"]

    out, rejected = [], {}
    for i, r in df.reset_index(drop=True).iterrows():
        text = str(r.get(tcol, "")).strip()
        rating = pd.to_numeric(r.get(rcol, ""), errors="coerce") if rcol else float("nan")
        reason = None
        if len(text) < 3:
            reason = "review text is empty"
        elif pd.isna(rating) or not (1 <= rating <= 5):
            reason = "rating is missing or not between 1 and 5"
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        out.append({"id": str(r.get(icol, "")).strip() or f"R{i + 1:04d}",
                    "app": (str(r.get(acol, "")).strip() if acol else "") or default_app,
                    "date": str(r.get(dcol, "")).strip() if dcol else "",
                    "title": str(r.get(ticol, "")).strip()[:200] if ticol else "",
                    "text": text[:1500], "rating": int(round(rating))})
    reviews = pd.DataFrame(out)
    total = len(df)
    notes = [f"{n} row{'s' if n != 1 else ''} skipped because the {why}." for why, n in rejected.items()]
    if total and len(reviews) / total < 0.5:
        raise FanPulseError(f"Only {len(reviews)} of {total} rows look like usable reviews.",
                            "This usually means the wrong file or the wrong columns. Check that each row has review text and a 1–5 rating.")
    if len(reviews) < MIN_REVIEWS:
        raise FanPulseError(f"Only {len(reviews)} usable reviews were found. FanPulse needs at least {MIN_REVIEWS} to rank problems reliably.",
                            "Add more reviews to the file, or use the sample data to see how it works.")
    if reviews["id"].duplicated().any():
        reviews["id"] = [f"R{i + 1:04d}" for i in range(len(reviews))]
        notes.append("Some review IDs were repeated, so FanPulse renumbered them.")
    return reviews, headlines, notes


def pick_sample(reviews, my_app, competitor, limit):
    mine = reviews[reviews["app"] == my_app]
    comp = reviews[reviews["app"] == competitor] if competitor else reviews.iloc[0:0]
    if comp.empty:
        return mine.head(limit)
    n_comp = min(len(comp), max(limit // 3, 1))
    n_mine = min(len(mine), limit - n_comp)
    return pd.concat([mine.head(n_mine), comp.head(limit - n_mine)])


# ---------------------------------------------------------------- AI calls
class AIError(Exception):
    pass


def call_gemini(api_key, model, system, user, max_tokens=4096, retries=3):
    body = {"contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    last = ""
    for attempt in range(retries):
        try:
            r = requests.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": api_key},
                              json=body, timeout=90)
        except requests.RequestException as e:
            last = f"network problem ({type(e).__name__})"
            time.sleep(2 * (attempt + 1) ** 2)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            last = {429: "the free AI quota is busy (rate limit)"}.get(r.status_code, f"the AI service is overloaded ({r.status_code})")
            time.sleep(3 * (attempt + 1) ** 2)
            continue
        if r.status_code in (400, 401, 403, 404):
            raise AIError(f"the AI service rejected the request ({r.status_code})")
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
            raise AIError("the AI declined to answer this batch")
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        m = re.search(r"[\[{][\s\S]*[\]}]", text or "")
        if not m:
            last = "the AI returned an empty or unreadable answer"
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            last = "the AI returned malformed data"
            continue
    raise AIError(last or "the AI service did not respond")


TAGGER_SYSTEM = f"""You classify app store reviews for a product analytics team. For EACH review return one tag object.
Allowed values:
funnel_stage: {', '.join(STAGES)}
theme: {', '.join(THEMES)}
sentiment: {', '.join(SENTIMENTS)}
severity: integer 1-5 (1 praise/minor, 3 real friction, 5 money stuck, account locked or user leaving)
churn_risk: {', '.join(CHURN)}
urgent: true only for money the user cannot access, a locked/restricted account, possible fraud, or problem-gambling signals
confidence: 0-1, how sure you are of stage and theme
issue_summary: max 18 words, neutral
evidence: an exact phrase copied from the review, max 12 words
Judge from the text, not just the stars. Never invent details.
Return JSON: {{"tags": [{{"id": "...", "funnel_stage": "...", "theme": "...", "sentiment": "...", "severity": 1, "churn_risk": "...", "urgent": false, "confidence": 0.9, "issue_summary": "...", "evidence": "..."}}]}}"""

CRITIC_SYSTEM = f"""You are a senior analyst double-checking tags a junior made on app reviews. You only see tags that were low-confidence or where stars and text disagree.
For each, decide "agree" or "override". Same allowed labels: funnel_stage {STAGES}; theme {THEMES}; sentiment {SENTIMENTS}; churn_risk {CHURN}; severity 1-5; urgent true/false.
Return JSON: {{"verdicts": [{{"id": "...", "verdict": "agree", "reason": "one sentence", "tag": {{ same fields as the junior tag }} }}]}}"""


def _norm(t):
    o = {}
    for k, allowed in (("funnel_stage", STAGES), ("theme", THEMES), ("sentiment", SENTIMENTS), ("churn_risk", CHURN)):
        v = str(t.get(k, "")).strip().lower()
        if v not in allowed:
            raise ValueError(k)
        o[k] = v
    sev = int(round(float(t.get("severity", 0))))
    if not 1 <= sev <= 5:
        raise ValueError("severity")
    o["severity"] = sev
    try:
        o["confidence"] = max(0.0, min(1.0, float(t.get("confidence", 0.5))))
    except (TypeError, ValueError):
        o["confidence"] = 0.5
    o["urgent"] = str(t.get("urgent", "")).lower() == "true" or t.get("urgent") is True
    o["issue_summary"] = str(t.get("issue_summary", ""))[:180]
    o["evidence"] = str(t.get("evidence", ""))[:140]
    return o


def tag_reviews(api_key, sample, progress=lambda done, total, msg: None, call=call_gemini):
    rows = sample.to_dict("records")
    tags, failed = {}, []
    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    for b_i, batch in enumerate(batches):
        progress(b_i, len(batches), f"Reading reviews {b_i * BATCH_SIZE + 1}–{b_i * BATCH_SIZE + len(batch)} of {len(rows)}")
        if b_i:
            time.sleep(CALL_GAP_SECONDS)
        payload = [{"id": r["id"], "app": r["app"], "stars": r["rating"], "title": r["title"], "text": r["text"]} for r in batch]
        try:
            res = call(api_key, TAGGER_MODEL, TAGGER_SYSTEM, json.dumps(payload, ensure_ascii=False), 6000)
            got = {str(t.get("id")): t for t in (res.get("tags", []) if isinstance(res, dict) else res)}
        except AIError as e:
            failed += [(r["id"], str(e)) for r in batch]
            continue
        for r in batch:
            t = got.get(r["id"])
            try:
                tags[r["id"]] = _norm(t or {})
            except (ValueError, TypeError):
                failed.append((r["id"], "the AI answer for this review was incomplete"))
    progress(len(batches), len(batches), "Tagging done")
    return tags, failed


def second_opinion(api_key, sample, tags, call=call_gemini):
    by_id = {r["id"]: r for r in sample.to_dict("records")}
    flagged = []
    for rid, t in tags.items():
        r = by_id[rid]
        mismatch = (r["rating"] >= 4 and t["sentiment"] == "negative") or (r["rating"] <= 2 and t["sentiment"] == "positive")
        if t["confidence"] < CONFIDENCE_THRESHOLD or mismatch:
            flagged.append(rid)
    flagged = flagged[:25]
    audit = {"flagged": len(flagged), "agree": 0, "override": 0, "failed": 0, "examples": []}
    if not flagged:
        return tags, audit
    payload = [{"id": rid, "stars": by_id[rid]["rating"], "text": by_id[rid]["text"], "junior_tag": tags[rid]} for rid in flagged]
    time.sleep(CALL_GAP_SECONDS)
    try:
        res = call(api_key, CRITIC_MODEL, CRITIC_SYSTEM, json.dumps(payload, ensure_ascii=False), 4000)
        verdicts = {str(v.get("id")): v for v in res.get("verdicts", [])}
    except AIError:
        audit["failed"] = len(flagged)
        return tags, audit
    for rid in flagged:
        v = verdicts.get(rid)
        if not v or v.get("verdict") not in ("agree", "override"):
            audit["failed"] += 1
            continue
        if v["verdict"] == "override":
            try:
                new = _norm(v.get("tag", {}))
            except (ValueError, TypeError):
                audit["failed"] += 1
                continue
            audit["examples"].append({"id": rid, "before": THEME_LABEL[tags[rid]["theme"]], "after": THEME_LABEL[new["theme"]],
                                      "reason": str(v.get("reason", ""))[:200]})
            tags[rid] = new
            audit["override"] += 1
        else:
            audit["agree"] += 1
    return tags, audit


# ---------------------------------------------------------------- numbers (code, not AI)
def build_table(sample, tags):
    df = sample.copy()
    for k in ["funnel_stage", "theme", "sentiment", "severity", "churn_risk", "urgent", "confidence", "issue_summary", "evidence"]:
        df[k] = df["id"].map(lambda i: tags.get(i, {}).get(k))
    df["tagged"] = df["id"].isin(tags.keys())
    df["owner"] = df["theme"].map(lambda t: OWNER.get(t, "") if t else "Manual review")
    return df


def compute_stats(table, my_app, competitor):
    t = table[table["tagged"]]
    def app_stats(app):
        g = t[t["app"] == app]
        neg = g[g["sentiment"].isin(["negative", "mixed"])]
        n = max(len(g), 1)
        return {"reviews": len(g), "avg_rating": round(g["rating"].mean(), 2) if len(g) else 0,
                "pct_negative": round(100 * len(neg) / n, 1),
                "neg_by_stage": {s: round(100 * (neg["funnel_stage"] == s).sum() / n, 1) for s in STAGES},
                "urgent": int(g["urgent"].fillna(False).astype(bool).sum())}
    mine = t[t["app"] == my_app]
    negm = mine[mine["sentiment"].isin(["negative", "mixed"]) & (mine["theme"] != "positive_experience")]
    impact = (negm.assign(churn_w=negm["churn_risk"].map({"low": 1, "medium": 1.5, "high": 2}))
              .groupby("theme").agg(reviews=("id", "count"), avg_severity=("severity", "mean"), churn_w=("churn_w", "mean")))
    impact["impact"] = impact["reviews"] * impact["avg_severity"] * impact["churn_w"]
    impact = impact.sort_values("impact", ascending=False).head(5)
    pains = []
    for theme, r in impact.iterrows():
        q = negm[negm["theme"] == theme].sort_values("severity", ascending=False)
        pains.append({"theme": theme, "label": THEME_LABEL[theme], "reviews": int(r["reviews"]),
                      "avg_severity": round(float(r["avg_severity"]), 1), "owner": OWNER[theme],
                      "stage": STAGE_LABEL[q.iloc[0]["funnel_stage"]],
                      "quotes": [e for e in q["evidence"].tolist() if e][:3]})
    urgent = t[(t["app"] == my_app) & t["urgent"].fillna(False).astype(bool)].sort_values("severity", ascending=False)
    return {"my_app": my_app, "competitor": competitor, "mine": app_stats(my_app),
            "comp": app_stats(competitor) if competitor else None, "pains": pains,
            "urgent": urgent[["id", "rating", "date", "issue_summary", "evidence", "owner", "severity"]].to_dict("records"),
            "tagged": int(len(t)), "total": int(len(table))}


REPORTER_SYSTEM = """You write the weekly review summary for a product analyst. You receive numbers already calculated from tagged app reviews, the top pain points with real quotes, optional competitor numbers and optional brand news headlines.
Reason, do not just restate. Use only numbers present in the data. Quotes must come from the provided quotes. Plain, confident language for a non-technical product manager.
Return JSON:
{"headline": "one sentence, the single most important finding",
 "fix_first": {"title": "the one problem to fix first", "why": "1-2 sentences on why this one, using the numbers", "first_step": "one concrete action for next week"},
 "promise_vs_reality": {"promise": "", "reality": "", "gap": "", "recommendation": ""},
 "competitor_view": {"summary": "", "losing_on": [""], "winning_on": [""]},
 "ab_tests": [{"name": "", "hypothesis": "", "metric": ""}],
 "support_reply": {"issue": "", "reply": "short, empathetic, no refund promises"}}
If no headlines are given, set promise_vs_reality to null. If no competitor is given, set competitor_view to null. Give exactly 3 ab_tests."""


def write_report(api_key, stats, headlines, call=call_gemini):
    payload = {"stats": {k: v for k, v in stats.items() if k != "urgent"}, "urgent_count": len(stats["urgent"]),
               "brand_headlines": headlines[:15]}
    notes = []
    for model in (REPORTER_MODEL, FALLBACK_MODEL):
        try:
            time.sleep(CALL_GAP_SECONDS)
            rep = call(api_key, model, REPORTER_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str), 8000)
            if isinstance(rep, dict) and rep.get("headline") and rep.get("fix_first"):
                if model != REPORTER_MODEL:
                    notes.append("The main writing model was busy, so a backup model wrote this summary.")
                return rep, notes
        except AIError as e:
            notes.append(f"Summary model unavailable: {e}.")
    # last resort: numbers-only summary
    top = stats["pains"][0] if stats["pains"] else None
    rep = {"headline": (f"{top['label']} is the biggest problem this week ({top['reviews']} reviews)." if top
                        else "No clear problem stood out this week."),
           "fix_first": {"title": top["label"] if top else "No single problem", "why": "Ranked by number of reviews × severity × churn risk.",
                         "first_step": f"Share these reviews with the {top['owner']}." if top else ""},
           "promise_vs_reality": None, "competitor_view": None, "ab_tests": [], "support_reply": None, "_fallback": True}
    notes.append("The AI summary could not be written, so this is a numbers-only summary. The rankings below are still accurate.")
    return rep, notes


def report_markdown(stats, rep, audit):
    m = stats["mine"]
    lines = [f"# FanPulse weekly summary: {stats['my_app']}", "", f"**{rep['headline']}**", "",
             f"## Fix this first: {rep['fix_first']['title']}", rep["fix_first"].get("why", ""), "",
             f"Next step: {rep['fix_first'].get('first_step', '')}", "",
             f"Reviews analysed: {stats['tagged']} · Negative: {m['pct_negative']}% · Urgent cases: {len(stats['urgent'])}", "",
             "## Top problems"]
    for i, p in enumerate(stats["pains"], 1):
        lines.append(f"{i}. {p['label']} ({p['reviews']} reviews, avg severity {p['avg_severity']}/5) - owner: {p['owner']}")
        for q in p["quotes"][:2]:
            lines.append(f'   - "{q}"')
    if rep.get("ab_tests"):
        lines += ["", "## Tests to run"] + [f"- {t['name']}: {t['hypothesis']} (metric: {t['metric']})" for t in rep["ab_tests"]]
    lines += ["", f"Second opinion: {audit['flagged']} unsure tags checked, {audit['override']} corrected."]
    return "\n".join(lines)


def analyse(api_key, sample, headlines, my_app, competitor, progress=lambda *a: None, call=call_gemini):
    t0 = time.time()
    tags, failed = tag_reviews(api_key, sample, progress, call)
    if len(tags) < max(MIN_REVIEWS // 2, 0.5 * len(sample)):
        raise FanPulseError("The AI service could not read enough of the reviews right now.",
                            f"Reason: {failed[0][1] if failed else 'unknown'}. Wait a minute and press Analyze reviews again.")
    progress(1, 1, "Double-checking unsure tags")
    tags, audit = second_opinion(api_key, sample, tags, call)
    table = build_table(sample, tags)
    stats = compute_stats(table, my_app, competitor)
    progress(1, 1, "Writing the summary")
    rep, notes = write_report(api_key, stats, headlines, call)
    return {"table": table, "stats": stats, "report": rep, "audit": audit, "failed": failed,
            "notes": notes, "seconds": round(time.time() - t0)}
