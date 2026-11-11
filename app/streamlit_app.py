"""Streamlit app over the dbt marts.

    streamlit run app/streamlit_app.py

Reads the DuckDB warehouse named by DUCKDB_PATH (default data/warehouse.duckdb), read-only.
All SQL lives in app/queries.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# So `streamlit run` finds the pipeline packages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import queries as q  # noqa: E402
from ingestion.config import get_settings  # noqa: E402

BAR = "#2a78d6"  # one series per chart, so one hue
SEQUENTIAL = ["#cde2fb", "#6da7ec", "#2a78d6", "#184f95", "#0d366b"]  # one hue, light to dark
ALL = "All sectors"

st.set_page_config(page_title="SEC 10-K Extraction", layout="wide")
settings = get_settings()


@st.cache_data(ttl=300)  # the warehouse changes at most weekly
def _load(db_path: str, name: str, *args):
    with q.connect(Path(db_path)) as con:
        return getattr(q, name)(con, *args)


def load(name: str, *args):
    return _load(str(settings.duckdb_path), name, *args)  # the path is part of the cache key


def billions(x) -> str:
    return "" if pd.isna(x) else f"${x / 1e9:,.1f}B"


def count(x) -> str:
    return "" if pd.isna(x) else f"{int(x):,}"


def pct(x) -> str:
    return "" if pd.isna(x) else f"{x:.1%}"


def year_bars(df: pd.DataFrame, field: str, title: str) -> alt.Chart:
    data = df.dropna(subset=[field]).assign(value=lambda d: d[field] / 1e9)
    return (
        alt.Chart(data, title=title)
        .mark_bar(color=BAR, cornerRadiusEnd=4, width={"band": 0.6})
        .encode(
            x=alt.X("fiscal_year:O", title="Fiscal year", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("value:Q", title="USD billions", axis=alt.Axis(grid=True, tickCount=5)),
            tooltip=[alt.Tooltip("fiscal_year:O", title="Fiscal year"),
                     alt.Tooltip("value:Q", title=f"{title} ($B)", format=",.1f")],
        )
        .properties(height=240)
    )  # fmt: skip


st.title("SEC 10-K filings, extracted by a local LLM")

try:
    model_list = load("models")
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:  # e.g. the marts don't exist yet
    st.error(f"Could not read the marts ({exc}). Run `cd dbt && dbt build` first.")
    st.stop()

if not model_list:
    st.warning("No extracted filings yet. Run the pipeline (see the README), then `dbt build`.")
    st.stop()

model = model_list[0]
if len(model_list) > 1:
    model = st.selectbox("Model", model_list)
ov = load("overview", model)

st.caption(
    f"{ov['filings']} annual reports (10-K) from {ov['companies']} companies in {ov['sectors']} "
    f"sectors, fiscal years {ov['first_year']} to {ov['last_year']}, read by `{model}` running "
    "locally. Every value passed schema validation; failures are quarantined, not guessed."
)
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Filings", f"{ov['filings']:,}")
c2.metric("Companies", f"{ov['companies']:,}")
c3.metric("LLM tasks valid", pct(ov["task_ok_rate"]))
c4.metric("Tasks quarantined", f"{ov['quarantined']:,}")
c5.metric("LLM cost", f"${ov['cost_usd'] or 0:,.2f}")

risks_tab, company_tab, quality_tab = st.tabs(["Risk trends", "Companies", "Extraction quality"])

with risks_tab:
    f1, f2, _ = st.columns([1, 1, 2])
    sector = f1.selectbox("Sector", [ALL, *load("sectors")])
    sector_arg = None if sector == ALL else sector
    heat = load("risk_heatmap", model, sector_arg)
    if heat.empty:
        st.info("No risk factors for this selection.")
    else:
        n_by_year = heat.groupby("fiscal_year")["n_filings"].first()
        st.caption(
            "Share of filings that list each risk category among their top risk factors. "
            "Filings per year: "
            + ", ".join(f"{y}: {int(n)}" for y, n in n_by_year.items())
            + ". Categories are assigned by the model from a fixed list of 11."
        )
        order = (heat.groupby("category_label")["share"].mean()
                 .sort_values(ascending=False).index.tolist())  # fmt: skip
        base = alt.Chart(heat).encode(
            x=alt.X("fiscal_year:O", title="Fiscal year", axis=alt.Axis(labelAngle=0)),
            y=alt.Y(
                "category_label:N",
                sort=order,
                title=None,
                axis=alt.Axis(labelLimit=220, labelOverlap=False),
            ),
        )
        cells = base.mark_rect(stroke="white", strokeWidth=2, cornerRadius=3).encode(
            color=alt.Color(
                "share:Q", title="Share of filings",
                scale=alt.Scale(domain=[0, 1], range=SEQUENTIAL),
                legend=alt.Legend(format=".0%", orient="bottom", gradientLength=240),
            ),
            tooltip=[alt.Tooltip("category_label:N", title="Risk category"),
                     alt.Tooltip("fiscal_year:O", title="Fiscal year"),
                     alt.Tooltip("share:Q", title="Share of filings", format=".0%"),
                     alt.Tooltip("n_filings_citing:Q", title="Filings citing it"),
                     alt.Tooltip("n_filings:Q", title="Filings that year")],
        )  # fmt: skip
        labels = base.mark_text(fontSize=12).encode(
            text=alt.Text("share:Q", format=".0%"),
            color=alt.condition("datum.share > 0.55", alt.value("white"), alt.value("#0b0b0b")),
        )
        st.altair_chart((cells + labels).properties(height=alt.Step(32)), width="stretch")

        with st.expander("Table view"):
            st.dataframe(
                heat.pivot(index="category_label", columns="fiscal_year", values="share")
                .reindex(order).map(pct),
                width="stretch",
            )  # fmt: skip

        labels_by_cat = dict(zip(heat["category_label"], heat["category"], strict=False))
        pick = st.selectbox("Show the risk titles behind a category", order)
        examples = load("risk_examples", model, labels_by_cat[pick], sector_arg)
        st.dataframe(
            examples.rename(columns={"ticker": "Ticker", "sector": "Sector",
                                     "fiscal_year": "Fiscal year", "risk_rank": "Rank",
                                     "title": "Risk factor (as extracted)"}),
            hide_index=True, width="stretch",
        )  # fmt: skip

with company_tab:
    comps = load("companies")
    labels = {r.ticker: f"{r.ticker} · {r.company_name}" for r in comps.itertuples()}
    ticker = st.selectbox("Company", list(labels), format_func=labels.get)
    filings = load("company_filings", model, ticker)
    if filings.empty:
        st.info("No validated record for this company: its filings may be quarantined.")
    else:
        left, right = st.columns(2)
        left.altair_chart(year_bars(filings, "revenue_usd", "Revenue"), width="stretch")
        right.altair_chart(year_bars(filings, "net_income_usd", "Net income"), width="stretch")
        table = pd.DataFrame({
            "Fiscal year": filings["fiscal_year"],
            "Revenue": filings["revenue_usd"].map(billions),
            "Net income": filings["net_income_usd"].map(billions),
            "Net margin": filings["net_margin"].map(pct),
            "Employees": filings["employee_count"].map(count),
            "Revenue growth": filings["revenue_growth_yoy"].map(pct),
            "Warnings": filings["n_warnings"],
        })  # fmt: skip
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption(
            "Empty cells: the model found no value, which the pipeline stores as null rather "
            "than guessing. Growth is shown only between consecutive years."
        )
    st.subheader("Risk factors")
    st.dataframe(
        load("company_risks", model, ticker).rename(columns={
            "fiscal_year": "Fiscal year", "risk_rank": "Rank", "title": "Risk factor",
            "category_label": "Category"}),
        hide_index=True, width="stretch",
    )  # fmt: skip

with quality_tab:
    st.subheader("Accuracy")
    scores = load("eval_scores", model)
    if scores.empty:
        st.info("No evaluation run yet: `python -m evaluation.evaluate`.")
    else:
        st.caption(
            "**gold**: 32 filings checked by hand against the filing. **xbrl**: every filing "
            "against the company's own inline XBRL tags. A value within 0.5% counts as correct; "
            "a wrong value counts against both precision and recall."
        )
        shown = scores.assign(
            field=scores["field"].str.replace("risk_category:macro", "risk_category (macro F1)"),
            **{c: scores[c].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")
               for c in ["precision", "recall", "f1", "exact_match"]},
        )  # fmt: skip
        st.dataframe(
            shown.rename(columns={"reference": "Reference", "field": "Field", "n": "n",
                                  "precision": "Precision", "recall": "Recall", "f1": "F1",
                                  "exact_match": "Exact match"}),
            hide_index=True, width="stretch",
        )  # fmt: skip

    st.subheader("Reliability and cost")
    summary = load("model_summary")
    row = summary[summary["model"] == model].iloc[0]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Retries", f"{int(row['n_retries']):,}")
    m2.metric("Avg seconds per task", f"{row['avg_task_latency_s']:.1f}")
    m3.metric("Tokens", f"{int(row['total_tokens'] or 0):,}")
    m4.metric("Revenue found", pct(row["revenue_coverage"]))

    st.subheader("Quarantined tasks")
    quarantine = load("quarantined", model)
    if quarantine.empty:
        st.success("None.")
    else:
        st.caption("Answers that failed validation on every attempt. They are kept with the "
                   "reason, never written to the tables above.")  # fmt: skip
        st.dataframe(
            quarantine.rename(columns={"ticker": "Ticker", "fiscal_year": "Fiscal year",
                                       "task": "Task", "attempts": "Attempts",
                                       "quarantine_reason": "Reason"}),
            hide_index=True, width="stretch",
        )  # fmt: skip
