"""Aggregate LLM call + adjudication stats for the model-router report UI (hamburger menu)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

try:
    from app.services.model_registry import composite_router_signal, composite_score_api_spec
except Exception:  # pragma: no cover
    composite_router_signal = None  # type: ignore[misc, assignment]
    composite_score_api_spec = None  # type: ignore[misc, assignment]


def _composite_spec_payload() -> dict[str, Any]:
    if composite_score_api_spec is None:
        return {}
    try:
        return composite_score_api_spec()
    except Exception:
        return {}


def _serialize_composite_breakdown(brk: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in brk.items():
        if isinstance(v, (int, float)):
            out[k] = round(float(v), 4)
        else:
            out[k] = v
    return out


def _stage_family_and_react_round(st: str) -> tuple[str, int | None]:
    s = (st or "").strip().lower()
    if s in ("plan", "planner"):
        return "planner", None
    if s.startswith("react_") and s[6:].isdigit():
        return "react", int(s[6:])
    if s.startswith("react_"):
        return "react", None
    return "other", None


def _stage_report_sort_key(stage: str) -> tuple[int, int, str]:
    """Order: plan → planner → react_1..n → other stages alphabetically."""
    s = (stage or "").strip().lower()
    if s == "plan":
        return (0, -2, stage or "")
    if s == "planner":
        return (0, -1, stage or "")
    if s.startswith("react_"):
        suf = s[6:]
        if suf.isdigit():
            return (0, int(suf), stage or "")
        return (0, 999, stage or "")
    return (1, 0, stage or "")


def _confidence_tier(quality_samples: int) -> str:
    if quality_samples >= 100:
        return "locked"
    if quality_samples >= 50:
        return "high"
    if quality_samples >= 10:
        return "medium"
    return "low"


def fetch_llm_router_report(window_days: int = 30) -> dict[str, Any]:
    """
    Returns per-stage model rows with call volume, adjudicated quality, composite ranking.
    Uses live aggregation on llm_calls (same window as model_performance_by_stage).
    """
    try:
        from app.chat_config import get_chat_config

        url = (get_chat_config().rag.database_url or "").strip()
    except Exception:
        url = ""
    if not url:
        out = _empty_report(window_days, "CHAT_RAG_DATABASE_URL not configured.")
        out["composite_spec"] = _composite_spec_payload()
        return out

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        out = _empty_report(window_days, "psycopg2 not available.")
        out["composite_spec"] = _composite_spec_payload()
        return out

    sql = """
        SELECT
            stage,
            model,
            MAX(provider) AS provider,
            COUNT(*)::bigint AS total_calls,
            COUNT(quality_score)::bigint AS quality_samples,
            AVG(CASE WHEN success = false THEN 1.0 ELSE 0.0 END)::float AS hard_error_rate,
            AVG(latency_ms) FILTER (WHERE success = true)::float AS avg_latency_ms,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE success = true)::float AS p95_latency_ms,
            AVG(cost_usd) FILTER (WHERE success = true)::float AS avg_cost_usd,
            AVG(quality_score) FILTER (WHERE quality_score IS NOT NULL)::float AS avg_quality,
            AVG(input_tokens) FILTER (WHERE success = true)::float AS avg_input_tokens,
            AVG(output_tokens) FILTER (WHERE success = true)::float AS avg_output_tokens
        FROM llm_calls
        WHERE ts > NOW() - make_interval(days => %s)
        GROUP BY stage, model
        ORDER BY stage ASC, model ASC
    """

    rows: list[dict[str, Any]] = []
    try:
        from app.services.cost_model import get_rates

        conn = psycopg2.connect(url)
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, (max(1, min(window_days, 365)),))
            for r in cur.fetchall():
                qs = int(r["quality_samples"] or 0)
                st = str(r["stage"] or "")
                prov = str(r["provider"] or "").strip() or "unknown"
                mdl = str(r["model"] or "").strip() or "unknown"
                avg_in = r.get("avg_input_tokens")
                avg_out = r.get("avg_output_tokens")
                avg_in_f = float(avg_in) if avg_in is not None else None
                avg_out_f = float(avg_out) if avg_out is not None else None
                in_rate, out_rate = get_rates(prov, mdl)
                list_price: float | None = None
                if avg_in_f is not None or avg_out_f is not None:
                    list_price = (max(0.0, avg_in_f or 0.0) / 1000.0) * in_rate + (
                        max(0.0, avg_out_f or 0.0) / 1000.0
                    ) * out_rate

                comp = 0.0
                brk_ser: dict[str, Any] = {}
                if composite_router_signal is not None:
                    try:
                        comp, brk = composite_router_signal(
                            {
                                "avg_quality": r.get("avg_quality"),
                                "hard_error_rate": r.get("hard_error_rate"),
                                "p95_latency_ms": r.get("p95_latency_ms"),
                                "avg_cost_usd": r.get("avg_cost_usd"),
                                "stage": st,
                            },
                            stage=st,
                        )
                        brk_ser = _serialize_composite_breakdown(brk)
                    except Exception:
                        comp = 0.0
                        brk_ser = {}
                rows.append(
                    {
                        "stage": str(r["stage"] or ""),
                        "model": str(r["model"] or ""),
                        "provider": str(r["provider"] or "").strip() or None,
                        "total_calls": int(r["total_calls"] or 0),
                        "quality_samples": qs,
                        "avg_quality": round(float(r["avg_quality"]), 4) if r.get("avg_quality") is not None else None,
                        "avg_latency_ms": int(r["avg_latency_ms"]) if r.get("avg_latency_ms") is not None else None,
                        "p95_latency_ms": int(r["p95_latency_ms"]) if r.get("p95_latency_ms") is not None else None,
                        "hard_error_rate": round(float(r["hard_error_rate"] or 0), 4),
                        "avg_cost_usd": round(float(r["avg_cost_usd"]), 6) if r.get("avg_cost_usd") is not None else None,
                        "avg_input_tokens": round(avg_in_f, 1) if avg_in_f is not None else None,
                        "avg_output_tokens": round(avg_out_f, 1) if avg_out_f is not None else None,
                        "usd_per_1k_input": round(float(in_rate), 6),
                        "usd_per_1k_output": round(float(out_rate), 6),
                        "avg_list_price_usd": round(float(list_price), 6) if list_price is not None else None,
                        "composite_score": round(comp, 4),
                        "composite_breakdown": brk_ser,
                        "confidence": _confidence_tier(qs),
                    }
                )
        finally:
            conn.close()
    except Exception as e:
        err = str(e)
        logger.warning("fetch_llm_router_report failed: %s", err)
        out = _empty_report(window_days, err[:500])
        out["composite_spec"] = _composite_spec_payload()
        return out

    # Group by stage, sort models inside each stage by composite (preferred first)
    stage_map: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        st = row["stage"] or "unknown"
        stage_map.setdefault(st, []).append(row)
    for st in stage_map:
        stage_map[st].sort(key=lambda x: (-(x["composite_score"] or 0), -(x["quality_samples"] or 0), x["model"]))

    stages_out = []
    for st in sorted(stage_map.keys(), key=_stage_report_sort_key):
        fam, rr = _stage_family_and_react_round(st)
        stages_out.append(
            {"stage": st, "stage_family": fam, "react_round": rr, "models": stage_map[st]}
        )

    return {
        "ok": True,
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warning": None,
        "stages": stages_out,
        "thompson": _thompson_meta(),
        "roster_enabled": _roster_enabled_snapshot(),
    }


def _empty_report(window_days: int, warning: str) -> dict[str, Any]:
    return {
        "ok": False,
        "window_days": window_days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "warning": warning,
        "stages": [],
        "thompson": _thompson_meta(),
        "roster_enabled": _roster_enabled_snapshot(),
        "composite_spec": {},
    }


def _thompson_meta() -> dict[str, Any]:
    try:
        from app.services.model_registry import (
            CIRCUIT_BREAKER_24H,
            CIRCUIT_BREAKER_ERROR,
            EXPLORATION_INTERVAL,
        )
    except Exception:
        EXPLORATION_INTERVAL = 20
        CIRCUIT_BREAKER_ERROR = 0.20
        CIRCUIT_BREAKER_24H = 0.15

    return {
        "title": "Thompson sampling (Beta bandit)",
        "summary": (
            "For each stage, eligible models get a Beta posterior: benchmark priors blended with "
            "pseudo-observations from the composite score (quality, errors, p95 latency, avg cost — "
            "see “Composite score” above). Each pick draws one Beta sample per model; highest wins. "
            "ReAct uses separate stages react_1…react_4: PG stats and composite caps are per round so "
            "early rounds don’t share the same bandit posterior as late rounds. "
            f"Every {EXPLORATION_INTERVAL} turns the least-sampled model is forced so new providers "
            "aren’t starved."
        ),
        "exploration_interval_turns": EXPLORATION_INTERVAL,
        "circuit_breaker_hard_error_max": CIRCUIT_BREAKER_ERROR,
        "circuit_breaker_24h_error_max": CIRCUIT_BREAKER_24H,
        "confidence_legend": {
            "low": "Fewer than 10 scored calls — prior + exploration dominates; shaded as low data.",
            "medium": "10–49 scored calls — observations blend with priors.",
            "high": "50–99 scored calls — mostly data-driven.",
            "locked": "100+ scored calls — strongest exploitation; still probed occasionally.",
        },
    }


def _roster_enabled_snapshot() -> list[dict[str, str]]:
    try:
        from app.services.model_registry import MODEL_ROSTER

        out = []
        for mid, spec in sorted(MODEL_ROSTER.items()):
            if not spec.enabled:
                continue
            out.append(
                {
                    "model_id": mid,
                    "display_name": spec.display_name,
                    "provider": spec.provider,
                }
            )
        return out
    except Exception:
        return []
