"""Score walk-forward FreqAI predictions against the later realized market move."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import pandas as pd


def score_pair(userdir: Path, model_dir: Path, pair: str, threshold_pct: float, fee_pct: float) -> dict:
    base, quote = pair.split("/", maxsplit=1)
    prediction_dir = model_dir / "backtesting_predictions"
    prediction_files = sorted(prediction_dir.glob(f"cb_{base.lower()}_*_prediction.feather"))
    if not prediction_files:
        return {"pair": pair, "error": f"Нет предсказаний в {prediction_dir}"}

    data_dir = userdir / "data" / "binance"
    candle_file = data_dir / f"{base}_{quote.replace(':', '_')}-15m.feather"
    if not candle_file.is_file():
        candidates = list(data_dir.glob(f"{base}_*-15m.feather"))
        candle_file = next((path for path in candidates if quote.split(":")[0] in path.stem), None)
    if not candle_file or not Path(candle_file).is_file():
        return {"pair": pair, "error": f"Нет 15m OHLCV-файла в {data_dir}"}

    candles = pd.read_feather(candle_file, columns=["date", "close"])
    candles["date"] = pd.to_datetime(candles["date"], utc=True)
    candles = candles.sort_values("date").drop_duplicates("date")
    candles["realized_return"] = candles["close"].shift(-12) / candles["close"] - 1
    actual = candles.set_index("date")["realized_return"]

    frames = []
    for path in prediction_files:
        frame = pd.read_feather(path)
        required = {"date", "&-forward_return", "do_predict"}
        if required.issubset(frame.columns):
            frames.append(frame[["date", "&-forward_return", "do_predict"]])
    if not frames:
        return {"pair": pair, "error": "В файлах FreqAI отсутствуют колонки прогноза"}

    predictions = pd.concat(frames, ignore_index=True)
    predictions["date"] = pd.to_datetime(predictions["date"], utc=True)
    predictions = predictions.drop_duplicates("date", keep="last").set_index("date")
    predictions["realized_return"] = actual.reindex(predictions.index)
    mask = (
        (predictions["do_predict"] == 1)
        & predictions["&-forward_return"].notna()
        & predictions["realized_return"].notna()
        & (predictions.index >= candles["date"].min())
    )
    valid = predictions.loc[mask].copy()
    if valid.empty:
        return {"pair": pair, "error": "Нет совпавших проверенных прогнозов и фактических свечей"}

    predicted = valid["&-forward_return"].astype(float)
    realized = valid["realized_return"].astype(float)
    signal_mask = predicted >= threshold_pct / 100
    signals = valid.loc[signal_mask]
    mae_pct = float((predicted - realized).abs().mean() * 100)
    rmse_pct = float(math.sqrt(((predicted - realized) ** 2).mean()) * 100)
    direction_accuracy = float(((predicted >= 0) == (realized >= 0)).mean() * 100)
    fee_beat_rate = float((signals["realized_return"] > fee_pct / 100).mean() * 100) if len(signals) else None
    average_signal_return = (
        float(signals["realized_return"].mean() * 100) if len(signals) else None
    )

    result = {
        "pair": pair,
        "observations": len(valid),
        "mae_pct": mae_pct,
        "rmse_pct": rmse_pct,
        "direction_accuracy_pct": direction_accuracy,
        "signals": len(signals),
        "signal_fee_beat_pct": fee_beat_rate,
        "signal_avg_realized_pct": average_signal_return,
    }
    if len(valid) < 500 or len(signals) < 30:
        result["verdict"] = "МАЛО НАБЛЮДЕНИЙ"
    elif fee_beat_rate is None or fee_beat_rate <= 50 or average_signal_return is None or average_signal_return <= fee_pct / 100:
        result["verdict"] = "ПРЕИМУЩЕСТВО НЕ ПОДТВЕРЖДЕНО"
    else:
        result["verdict"] = "КАНДИДАТ ДЛЯ PAPER НАБЛЮДЕНИЯ"
    return result


def print_backtest_summary(results_dir: Path, pairs: list[str], identifier: str) -> dict[str, dict]:
    archives = sorted(results_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not archives:
        print("Торговый отчёт Freqtrade не найден.")
        return {}

    by_pair: dict[str, dict] = {}
    used_archives = []
    for path in archives:
        with ZipFile(path) as archive:
            config_name = next((name for name in archive.namelist() if name.endswith("_config.json")), None)
            stats_name = next((name for name in archive.namelist() if name.endswith(".json") and not name.endswith(("_config.json", "_metadata.json"))), None)
            if not config_name or not stats_name:
                continue
            config = json.loads(archive.read(config_name))
            if config.get("freqai", {}).get("identifier") != identifier:
                continue
            payload = json.loads(archive.read(stats_name))
        used_archives.append(path.name)
        strategy_result = next(iter(payload.get("strategy", {}).values()), {})
        rows = {item.get("key"): item for item in strategy_result.get("results_per_pair", [])}
        for pair in pairs:
            if pair not in by_pair and pair in rows:
                by_pair[pair] = rows[pair]

    if not used_archives:
        print("Backtest отчёты для этого запуска не найдены.")
        return {}
    print(f"\nBacktest отчёты ({len(used_archives)} архивов)")
    print("Пара         Сделки    PnL %    Win %    Profit factor    Max DD %")
    for pair in pairs:
        row = by_pair.get(pair)
        if not row:
            print(f"{pair:<12} нет строки в отчёте")
            continue
        print(
            f"{pair:<12} {row.get('trades', 0):>6}"
            f" {row.get('profit_total_pct', 0):>8.2f}"
            f" {row.get('winrate', 0) * 100:>8.1f}"
            f" {row.get('profit_factor', 0):>15.2f}"
            f" {row.get('max_drawdown_account', 0) * 100:>10.2f}"
        )
    return by_pair


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--userdir", type=Path, default=Path("/freqtrade/user_data"))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--config-path", type=Path)
    parser.add_argument("--pairs", nargs="+", required=True)
    parser.add_argument("--threshold-pct", type=float, default=0.5)
    parser.add_argument("--fee-pct", type=float, default=0.25)
    args = parser.parse_args()

    print("Walk-forward проверка прогноза на следующих 12 свечах (3 часа):")
    print("Пара         Свечи  MAE %  RMSE %  Направление %  Сигналы  После издержек %  Средний ход %  Оценка")
    forecasts = {}
    for pair in args.pairs:
        result = score_pair(args.userdir, args.model_dir, pair, args.threshold_pct, args.fee_pct)
        if "error" in result:
            print(f"{pair:<12} {result['error']}")
            forecasts[pair] = {"status": "not_validated", "reason": result["error"]}
            continue
        forecasts[pair] = result
        fee_rate = result["signal_fee_beat_pct"]
        avg = result["signal_avg_realized_pct"]
        fee_rate_text = f"{fee_rate:.1f}" if fee_rate is not None else "—"
        avg_text = f"{avg:.3f}" if avg is not None else "—"
        print(
            f"{pair:<12} {result['observations']:>6} {result['mae_pct']:>6.3f}"
            f" {result['rmse_pct']:>7.3f} {result['direction_accuracy_pct']:>14.1f}"
            f" {result['signals']:>8} {fee_rate_text:>17} {avg_text:>14}"
            f"  {result['verdict']}"
        )
    trading = print_backtest_summary(args.userdir / "backtest_results", args.pairs, args.model_dir.name)
    for pair, forecast in forecasts.items():
        stats = trading.get(pair, {})
        enough_forecasts = forecast.get("observations", 0) >= 500 and forecast.get("signals", 0) >= 30
        predictive_edge = (
            forecast.get("signal_fee_beat_pct") is not None
            and forecast.get("signal_fee_beat_pct", 0) > 52
            and forecast.get("signal_avg_realized_pct") is not None
            and forecast.get("signal_avg_realized_pct", 0) > args.fee_pct / 100
        )
        trading_edge = (
            stats.get("trades", 0) >= 30
            and stats.get("profit_total_pct", 0) > 0
            and stats.get("profit_factor", 0) > 1
            and stats.get("max_drawdown_account", 1) <= 0.10
        )
        if enough_forecasts and predictive_edge and trading_edge:
            forecast["status"] = "paper_eligible"
            forecast["reason"] = "Forecast and backtest filters passed; eligible for paper observation only"
        elif forecast.get("verdict") == "МАЛО НАБЛЮДЕНИЙ":
            forecast["status"] = "not_validated"
            forecast["reason"] = "Too few forecasts or signals to evaluate"
        else:
            forecast["status"] = "not_validated"
            forecast["reason"] = "Minimum predictive, profitability, profit-factor, or drawdown filter failed"
        forecast["backtest"] = {
            "trades": stats.get("trades", 0),
            "profit_total_pct": stats.get("profit_total_pct", 0),
            "profit_factor": stats.get("profit_factor", 0),
            "max_drawdown_pct": stats.get("max_drawdown_account", 0) * 100,
        }
        print(f"{pair}: {forecast['status']} — {forecast['reason']}")

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "identifier": args.model_dir.name,
        "timeframe": "15m",
        "horizon_candles": 12,
        "horizon_hours": 3,
        "fee_pct": args.fee_pct,
        "strategy_sha256": hashlib.sha256(
            (args.userdir / "strategies" / "TredinkForecastStrategy.py").read_bytes()
        ).hexdigest(),
        "config_sha256": hashlib.sha256(
            (args.config_path or (args.userdir / "tredink.sample.json")).read_bytes()
        ).hexdigest(),
        "pairs": forecasts,
    }
    args.userdir.mkdir(parents=True, exist_ok=True)
    report_path = args.userdir / "tredink-validation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"\nValidation and signal-gate report saved: {report_path}")
    print("\nОценка сделана на walk-forward прогнозах; прошедший тест не гарантирует будущую доходность.")


if __name__ == "__main__":
    main()
