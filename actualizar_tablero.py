from datetime import date
import json
import os
import time
from typing import Any, Dict, List, Optional
import pandas as pd
import requests

API_VERSION = "v22.0"
SINCE_DATE = "2026-08-01"
UNTIL_DATE = date.today().isoformat()
OUTPUT_JSON = "tablero_leads_motoria_jetour.json"

ACCOUNTS = [
    {
        "brand": "Motoria",
        "account_id": "1503346277819510",
        "access_token": os.getenv("MOTORIA_ACCESS_TOKEN", ""),
    },
    {
        "brand": "Jetour",
        "account_id": "27099064126446848",
        "access_token": os.getenv("JETOUR_ACCESS_TOKEN", ""),
    },
]

# Evita inflar o duplicar leads entre alias equivalentes de formulario en Meta
FORM_LEAD_ACTION_PRIORITY = [
    "onsite_conversion.lead_grouped",
    "lead",
]

MSG_ACTION_TYPES = [
    "onsite_conversion.messaging_conversation_started_7d",
    "messaging_conversation_started_7d",
]

INSIGHT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "objective",
    "date_start",
    "spend",
    "impressions",
    "reach",
    "clicks",
    "cpc",
    "cpm",
    "ctr",
    "actions",
]


def _graph_get(
    url: str, params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    resp = requests.get(url, params=params, timeout=60)
    if not resp.ok:
        try:
            detalle = resp.json()
        except ValueError:
            detalle = resp.text
        raise RuntimeError(
            f"Meta Graph API error ({resp.status_code}) en {url}: {detalle}"
        )
    return resp.json()


def get_campaigns_metadata(
    account_id: str, access_token: str
) -> Dict[str, Dict[str, Any]]:
    act_id = (
        account_id if account_id.startswith("act_") else f"act_{account_id}"
    )
    url = f"https://graph.facebook.com/{API_VERSION}/{act_id}/campaigns"
    params: Optional[Dict[str, Any]] = {
        "access_token": access_token,
        "fields": "id,name,status,effective_status,objective,daily_budget,lifetime_budget",
        "limit": 200,
    }
    meta_map: Dict[str, Dict[str, Any]] = {}
    while url:
        payload = _graph_get(url, params=params)
        for c in payload.get("data", []):
            meta_map[c["id"]] = {
                "effective_status": c.get("effective_status"),
                "daily_budget": (
                    round(float(c["daily_budget"]) / 100, 2)
                    if c.get("daily_budget")
                    else None
                ),
                "lifetime_budget": (
                    round(float(c["lifetime_budget"]) / 100, 2)
                    if c.get("lifetime_budget")
                    else None
                ),
            }
        url = payload.get("paging", {}).get("next")
        params = None
        time.sleep(0.2)
    return meta_map


def get_daily_insights(
    account_id: str, access_token: str, since: str, until: str
) -> List[Dict[str, Any]]:
    act_id = (
        account_id if account_id.startswith("act_") else f"act_{account_id}"
    )
    url = f"https://graph.facebook.com/{API_VERSION}/{act_id}/insights"
    params: Optional[Dict[str, Any]] = {
        "access_token": access_token,
        "level": "campaign",
        "fields": ",".join(INSIGHT_FIELDS),
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": 1,
        "limit": 200,
    }
    rows: List[Dict[str, Any]] = []
    while url:
        payload = _graph_get(url, params=params)
        rows.extend(payload.get("data", []))
        url = payload.get("paging", {}).get("next")
        params = None
        time.sleep(0.2)
    return rows


def extract_unbiased_leads(actions_list: List[Dict[str, Any]]) -> int:
    if not actions_list:
        return 0
    action_map: Dict[str, float] = {}
    for item in actions_list:
        atype = item.get("action_type")
        if atype in FORM_LEAD_ACTION_PRIORITY:
            try:
                action_map[atype] = action_map.get(atype, 0.0) + float(
                    item.get("value", 0) or 0
                )
            except (ValueError, TypeError):
                pass

    for key in FORM_LEAD_ACTION_PRIORITY:
        if key in action_map and action_map[key] > 0:
            return int(round(action_map[key]))
    return 0


def extract_messages(actions_list: List[Dict[str, Any]]) -> int:
    if not actions_list:
        return 0
    for key in MSG_ACTION_TYPES:
        for item in actions_list:
            if item.get("action_type") == key:
                try:
                    val = float(item.get("value", 0) or 0)
                    if val > 0:
                        return int(round(val))
                except (ValueError, TypeError):
                    pass
    return 0


def calc_var(current: float, previous: float) -> Dict[str, Any]:
    diff_abs = round(current - previous, 2)
    diff_pct = (
        round(((current - previous) / previous) * 100, 1)
        if previous and previous > 0
        else None
    )
    return {"abs": diff_abs, "pct": diff_pct}


def prev_month_ym(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    dt = pd.Timestamp(year=y, month=m, day=1) - pd.DateOffset(months=1)
    return dt.strftime("%Y-%m")


def prev_month_same_day(dt: pd.Timestamp) -> str:
    return (dt - pd.DateOffset(months=1)).strftime("%Y-%m-%d")


def build_monthly_comparison_for_scope(
    df_scope: pd.DataFrame, target_ym: str, max_dt: pd.Timestamp
) -> Dict[str, Any]:
    prev_ym = prev_month_ym(target_ym)
    cur_ym_str = max_dt.strftime("%Y-%m")
    cutoff_day = max_dt.day if target_ym == cur_ym_str else 31

    if df_scope.empty:
        return {
            "mes": target_ym,
            "mes_anterior": prev_ym,
            "dias_comparados": cutoff_day,
            "gasto_actual": 0.0,
            "gasto_mes_pasado": 0.0,
            "var_gasto": {"abs": 0.0, "pct": None},
            "leads_actual": 0,
            "leads_mes_pasado": 0,
            "var_leads": {"abs": 0, "pct": None},
            "cpl_actual": None,
            "cpl_mes_pasado": None,
            "var_cpl": {"abs": None, "pct": None},
        }

    g_cur = df_scope[
        (df_scope["ym"] == target_ym) & (df_scope["day"] <= cutoff_day)
    ]
    g_prev = df_scope[
        (df_scope["ym"] == prev_ym) & (df_scope["day"] <= cutoff_day)
    ]

    gasto_act = round(float(g_cur["spend"].sum()), 2)
    gasto_prev = round(float(g_prev["spend"].sum()), 2)
    leads_act = int(g_cur["leads"].sum())
    leads_prev = int(g_prev["leads"].sum())

    cpl_act = round(gasto_act / leads_act, 2) if leads_act > 0 else None
    cpl_prev = round(gasto_prev / leads_prev, 2) if leads_prev > 0 else None
    var_cpl = (
        calc_var(cpl_act, cpl_prev)
        if (cpl_act is not None and cpl_prev is not None)
        else {"abs": None, "pct": None}
    )

    return {
        "mes": target_ym,
        "mes_anterior": prev_ym,
        "dias_comparados": cutoff_day,
        "gasto_actual": gasto_act,
        "gasto_mes_pasado": gasto_prev,
        "var_gasto": calc_var(gasto_act, gasto_prev),
        "leads_actual": leads_act,
        "leads_mes_pasado": leads_prev,
        "var_leads": {
            "abs": leads_act - leads_prev,
            "pct": (
                round(((leads_act - leads_prev) / leads_prev) * 100, 1)
                if leads_prev > 0
                else None
            ),
        },
        "cpl_actual": cpl_act,
        "cpl_mes_pasado": cpl_prev,
        "var_cpl": var_cpl,
    }


def build_full_dashboard_json(
    since: str = SINCE_DATE, until: str = UNTIL_DATE
) -> Dict[str, Any]:
    raw_rows: List[Dict[str, Any]] = []
    accounts_ok: List[Dict[str, str]] = []
    accounts_failed: List[Dict[str, str]] = []

    for acc in ACCOUNTS:
        brand = acc["brand"]
        acc_id = acc["account_id"]
        token = acc["access_token"].strip()

        if not token:
            msg = f"Falta el secret/token para {brand} ({acc_id})"
            print(f"ERROR: {msg}")
            accounts_failed.append(
                {"brand": brand, "account_id": acc_id, "error": msg}
            )
            continue

        print(f"Extrayendo datos diarios de {brand} ({since} a {until})...")
        try:
            try:
                meta_map = get_campaigns_metadata(acc_id, token)
            except Exception as meta_exc:
                print(
                    f"Aviso metadata {brand} ({acc_id}): {meta_exc} -> usando datos de insights."
                )
                meta_map = {}

            insights = get_daily_insights(acc_id, token, since, until)
            accounts_ok.append({"brand": brand, "account_id": acc_id})

            for r in insights:
                cid = r.get("campaign_id")
                meta = meta_map.get(cid, {})
                spend = float(r.get("spend", 0) or 0)
                actions = r.get("actions", []) or []

                leads = extract_unbiased_leads(actions)
                msg_convos = extract_messages(actions)
                impressions = int(float(r.get("impressions", 0) or 0))
                reach = int(float(r.get("reach", 0) or 0))
                clicks = int(float(r.get("clicks", 0) or 0))
                cpl = round(spend / leads, 2) if leads > 0 else None

                raw_rows.append(
                    {
                        "fecha": r.get("date_start"),
                        "brand": brand,
                        "account_id": acc_id,
                        "campaign_id": cid,
                        "campaign_name": r.get("campaign_name"),
                        "objective": r.get("objective"),
                        "effective_status": meta.get("effective_status"),
                        "daily_budget": meta.get("daily_budget"),
                        "lifetime_budget": meta.get("lifetime_budget"),
                        "spend": round(spend, 2),
                        "leads": leads,
                        "cpl": cpl,
                        "messaging_conversations": msg_convos,
                        "impressions": impressions,
                        "reach": reach,
                        "clicks": clicks,
                        "ctr": (
                            round(float(r.get("ctr", 0) or 0), 2)
                            if r.get("ctr")
                            else None
                        ),
                    }
                )
        except Exception as acc_exc:
            print(f"ERROR en cuenta {brand} ({acc_id}): {acc_exc}")
            accounts_failed.append(
                {"brand": brand, "account_id": acc_id, "error": str(acc_exc)}
            )

    if not raw_rows:
        raise RuntimeError(
            "No se pudo extraer información de ninguna cuenta. Revisa los secrets MOTORIA_ACCESS_TOKEN y JETOUR_ACCESS_TOKEN."
        )

    df = pd.DataFrame(raw_rows)
    df["fecha_dt"] = pd.to_datetime(df["fecha"])
    df["ym"] = df["fecha"].str.slice(0, 7)
    df["day"] = df["fecha"].str.slice(8, 10).astype(int)

    max_dt = df["fecha_dt"].max()
    ref_date_str = max_dt.strftime("%Y-%m-%d")
    full_date_index = pd.date_range(
        start=df["fecha_dt"].min(), end=max_dt, freq="D"
    )

    filterable_months = sorted(
        [ym for ym in df["ym"].unique().tolist() if ym >= "2026-09"]
    )

    comparativo_por_mes: Dict[str, Dict[str, Any]] = {}
    scopes_map = {
        "Juntos": df,
        "Motoria": df[df["brand"] == "Motoria"],
        "Jetour": df[df["brand"] == "Jetour"],
    }

    for ym in filterable_months:
        comparativo_por_mes[ym] = {
            scope_name: build_monthly_comparison_for_scope(
                scope_df, ym, max_dt
            )
            for scope_name, scope_df in scopes_map.items()
        }

    serie_diaria_leads: List[Dict[str, Any]] = []
    for scope_name, scope_df in scopes_map.items():
        if scope_df.empty:
            continue
        g = (
            scope_df.groupby(["fecha_dt", "fecha"], as_index=False)
            .agg(
                leads=("leads", "sum"),
                spend=("spend", "sum"),
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
            )
            .set_index("fecha_dt")
            .reindex(full_date_index)
            .fillna(
                {"leads": 0, "spend": 0.0, "impressions": 0, "clicks": 0}
            )
        )
        g["fecha_dt"] = g.index
        g["fecha"] = g.index.strftime("%Y-%m-%d")
        g["leads"] = g["leads"].astype(int)
        g["spend"] = g["spend"].round(2)
        g["impressions"] = g["impressions"].astype(int)
        g["clicks"] = g["clicks"].astype(int)
        g["promedio_movil_7d"] = (
            g["leads"].rolling(window=7, min_periods=1).mean().round(2)
        )

        lookup = {
            r["fecha"]: {
                "leads": int(r["leads"]),
                "spend": round(float(r["spend"]), 2),
            }
            for _, r in g.iterrows()
        }

        for _, r in g.iterrows():
            f_str = r["fecha"]
            prev_m_str = prev_month_same_day(r["fecha_dt"])
            pm = lookup.get(prev_m_str, {"leads": 0, "spend": 0.0})
            cur_l = int(r["leads"])
            cur_s = round(float(r["spend"]), 2)
            prev_l = pm["leads"]
            prev_s = pm["spend"]

            serie_diaria_leads.append(
                {
                    "fecha": f_str,
                    "scope": scope_name,
                    "leads": cur_l,
                    "spend": cur_s,
                    "cpl_diario": (
                        round(cur_s / cur_l, 2) if cur_l > 0 else None
                    ),
                    "promedio_movil_7d": float(r["promedio_movil_7d"]),
                    "impressions": int(r["impressions"]),
                    "clicks": int(r["clicks"]),
                    "fecha_mes_pasado": prev_m_str,
                    "leads_mes_pasado": prev_l,
                    "var_leads_vs_mes_pasado": {
                        "abs": cur_l - prev_l,
                        "pct": (
                            round(((cur_l - prev_l) / prev_l) * 100, 1)
                            if prev_l > 0
                            else None
                        ),
                    },
                    "spend_mes_pasado": prev_s,
                    "var_spend_vs_mes_pasado": calc_var(cur_s, prev_s),
                }
            )

    comparacion_campanas: List[Dict[str, Any]] = []
    for (brand_name, cid, cname, obj, status, d_budget, l_budget), cg in df.groupby(
        [
            "brand",
            "campaign_id",
            "campaign_name",
            "objective",
            "effective_status",
            "daily_budget",
            "lifetime_budget",
        ],
        dropna=False,
    ):
        cg_full = (
            cg.groupby(["fecha_dt", "fecha"], as_index=False)
            .agg(
                leads=("leads", "sum"),
                spend=("spend", "sum"),
                impressions=("impressions", "sum"),
                clicks=("clicks", "sum"),
            )
            .set_index("fecha_dt")
            .reindex(full_date_index)
            .fillna(
                {"leads": 0, "spend": 0.0, "impressions": 0, "clicks": 0}
            )
        )
        cg_full["fecha"] = cg_full.index.strftime("%Y-%m-%d")
        cg_full["leads"] = cg_full["leads"].astype(int)
        cg_full["spend"] = cg_full["spend"].round(2)
        cg_full["impressions"] = cg_full["impressions"].astype(int)
        cg_full["clicks"] = cg_full["clicks"].astype(int)

        historial = [
            {
                "fecha": r["fecha"],
                "leads": int(r["leads"]),
                "spend": float(r["spend"]),
                "cpl": (
                    round(float(r["spend"]) / int(r["leads"]), 2)
                    if int(r["leads"]) > 0
                    else None
                ),
                "impressions": int(r["impressions"]),
                "clicks": int(r["clicks"]),
            }
            for _, r in cg_full.iterrows()
        ]

        total_leads = int(cg_full["leads"].sum())
        total_spend = round(float(cg_full["spend"].sum()), 2)

        comparacion_campanas.append(
            {
                "brand": brand_name,
                "campaign_id": cid,
                "campaign_name": cname,
                "objective": obj,
                "effective_status": None if pd.isna(status) else status,
                "daily_budget": None if pd.isna(d_budget) else float(d_budget),
                "lifetime_budget": (
                    None if pd.isna(l_budget) else float(l_budget)
                ),
                "total_leads": total_leads,
                "total_spend": total_spend,
                "cpl_promedio": (
                    round(total_spend / total_leads, 2)
                    if total_leads > 0
                    else None
                ),
                "historial_diario": historial,
            }
        )

    comparacion_campanas.sort(
        key=lambda x: (x["brand"], -x["total_leads"], -x["total_spend"])
    )

    return {
        "metadata": {
            "since": since,
            "until": until,
            "fecha_referencia": ref_date_str,
            "generated_at": date.today().isoformat(),
            "meses_disponibles": filterable_months,
            "scopes": ["Juntos", "Motoria", "Jetour"],
            "accounts_ok": accounts_ok,
            "accounts_failed": accounts_failed,
        },
        "comparativo_por_mes": comparativo_por_mes,
        "serie_diaria_leads": serie_diaria_leads,
        "comparacion_campanas_diaria": comparacion_campanas,
    }


if __name__ == "__main__":
    payload = build_full_dashboard_json(since=SINCE_DATE, until=UNTIL_DATE)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"JSON actualizado guardado en: {OUTPUT_JSON}")
    for ym, sc in payload.get("comparativo_por_mes", {}).items():
        print(
            f"Mes {ym} -> Motoria: {sc['Motoria']['leads_actual']} leads | Jetour: {sc['Jetour']['leads_actual']} leads"
        )
