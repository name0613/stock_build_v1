"""Create a sanitized, reproducible calibration snapshot for capital-aware-v7."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as parquet
from sqlalchemy import select

from backend.app.db import SessionLocal, init_db
from backend.app.models import AccumulationScore, CapitalAwareScore, Stock
from backend.app.scoring import CAPITAL_AWARE_FORMULA_HASH, CAPITAL_AWARE_SCORE_VERSION, FORMULA_HASH, SCORE_VERSION


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def calibration(raw_root: Path) -> dict[str, Any]:
    init_db()
    by_stock_date: dict[tuple[str, str], float] = {}
    raw_files = sorted((raw_root / "TaiwanStockPrice").glob("date=*/part-*.parquet"))
    for path in raw_files:
        for row in parquet.read_table(path, columns=["date", "stock_id", "Trading_money"]).to_pylist():
            money = row.get("Trading_money")
            if money is not None and float(money) > 0:
                by_stock_date[(str(row.get("stock_id")), str(row.get("date"))[:10])] = float(money)
    raw_by_stock: dict[str, list[float]] = defaultdict(list)
    for (stock_id, _), money in by_stock_date.items():
        raw_by_stock[stock_id].append(money)
    median_20d = {stock_id: statistics.median(values[-20:]) for stock_id, values in raw_by_stock.items() if len(values) >= 20}

    with SessionLocal() as db:
        stocks = {stock.stock_id: stock for stock in db.scalars(select(Stock).where(Stock.is_common_stock.is_(True))).all()}
        capital_rows = db.scalars(select(CapitalAwareScore).where(CapitalAwareScore.score_version == CAPITAL_AWARE_SCORE_VERSION)).all()
        latest_capital: dict[str, CapitalAwareScore] = {}
        for row in sorted(capital_rows, key=lambda item: (item.stock_id, item.source_date, item.calculated_at, item.id)):
            latest_capital[row.stock_id] = row
        stealth_rows = db.scalars(select(AccumulationScore).where(AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.score.is_not(None)).order_by(AccumulationScore.score.desc(), AccumulationScore.stock_id)).all()
        top20 = stealth_rows[:20]
        top50 = stealth_rows[:50]

        def capital_summary(rows: list[AccumulationScore]) -> list[dict[str, Any]]:
            result = []
            for row in rows:
                capital = latest_capital.get(row.stock_id)
                features = capital.features if capital else {}
                components = capital.components if capital else {}
                result.append({"stock_id": row.stock_id, "market": stocks.get(row.stock_id).market if row.stock_id in stocks else None, "stealth_score": row.score, "median_trading_value_20d": features.get("MedianTradingValue20D"), "estimated_institutional_net_value_20d": features.get("EstimatedInstitutionalNetValue20D"), "capital_scale_score": components.get("CapitalScaleScore"), "status": capital.status if capital else "CAPITAL_AWARE_NOT_SCORED"})
            return result

        representatives = {}
        for stock_id in ("2330", "2317", "2454", "1101"):
            capital = latest_capital.get(stock_id)
            representatives[stock_id] = {"market": stocks.get(stock_id).market if stock_id in stocks else None, "raw_price_sessions": len(raw_by_stock.get(stock_id, [])), "median_trading_value_20d_raw": median_20d.get(stock_id), "capital_aware_status": capital.status if capital else "CAPITAL_AWARE_NOT_SCORED", "capital_aware_components": capital.components if capital else {}, "eligibility_reasons": (capital.components or {}).get("eligibility_reasons", []) if capital else ["no_v7_snapshot"]}

        estimated_values = [
            float((row.features or {}).get("EstimatedInstitutionalNetValue20D"))
            for row in latest_capital.values()
            if (row.features or {}).get("EstimatedInstitutionalNetValue20D") is not None
        ]
        market_distributions: dict[str, dict[str, Any]] = {}
        for market in ("上市", "上櫃", "興櫃"):
            values = [median_20d[stock_id] for stock_id, stock in stocks.items() if stock.market == market and stock_id in median_20d]
            estimates = [
                float((latest_capital[stock_id].features or {}).get("EstimatedInstitutionalNetValue20D"))
                for stock_id, stock in stocks.items()
                if stock.market == market and stock_id in latest_capital and (latest_capital[stock_id].features or {}).get("EstimatedInstitutionalNetValue20D") is not None
            ]
            market_distributions[market] = {"formal_money_sample_count_20d": len(values), "formal_money_median": statistics.median(values) if values else None, "formal_money_p25": percentile(values, 0.25), "formal_money_p75": percentile(values, 0.75), "estimated_institutional_sample_count_20d": len(estimates), "estimated_institutional_median": statistics.median(estimates) if estimates else None, "estimated_institutional_p25": percentile(estimates, 0.25), "estimated_institutional_p75": percentile(estimates, 0.75)}

    all_values = list(median_20d.values())
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "source_revision": "local-raw-parquet", "score_version": CAPITAL_AWARE_SCORE_VERSION, "formula_hash": CAPITAL_AWARE_FORMULA_HASH, "legacy_score_version": SCORE_VERSION, "legacy_formula_hash": FORMULA_HASH, "raw_price_files": len(raw_files), "raw_unique_stock_date_rows": len(by_stock_date), "stocks_with_20d_formal_money": len(median_20d), "median_trading_value_20d_distribution": {"median": statistics.median(all_values) if all_values else None, "p25": percentile(all_values, 0.25), "p50": percentile(all_values, 0.50), "p75": percentile(all_values, 0.75), "p90": percentile(all_values, 0.90), "sample_count": len(all_values)}, "estimated_institutional_net_value_20d_distribution": {"median": statistics.median(estimated_values) if estimated_values else None, "p25": percentile(estimated_values, 0.25), "p50": percentile(estimated_values, 0.50), "p75": percentile(estimated_values, 0.75), "p90": percentile(estimated_values, 0.90), "sample_count": len(estimated_values)}, "market_distributions": market_distributions, "existing_s_top20": capital_summary(top20), "existing_s_top50": capital_summary(top50), "legacy_s_top20_summary": {"score_version": SCORE_VERSION, "numeric_score_count": len(stealth_rows), "top20_count": len(top20), "top50_count": len(top50), "note": "The current 2026-08-20 v6 run is fail-closed because the local provider snapshot lacks required current foreign/broker coverage; no numeric v6 rows were silently substituted."}, "representative_stocks": representatives, "calibration_note": "Fixed TWD breakpoints are selected from the available formal-money and estimated-institutional distributions, with conservative absolute gates so a high buy-ratio low-liquidity stock cannot qualify as large capital. The raw cache currently has limited 20D coverage, so thresholds require later re-calibration when broader formal-money history is available.", "secrets_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("deployment_evidence/CAPITAL_AWARE_CALIBRATION_EVIDENCE.json"))
    args = parser.parse_args()
    report = calibration(args.raw_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "stocks_with_20d_formal_money": report["stocks_with_20d_formal_money"], "secrets_included": False}, ensure_ascii=False))
