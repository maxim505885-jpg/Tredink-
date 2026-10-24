"""FreqAI strategy used by the Tredink market-analysis dashboard."""

from __future__ import annotations

import math

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from freqtrade.strategy import DecimalParameter, IStrategy, stoploss_from_absolute


class TredinkForecastStrategy(IStrategy):
    """Predict a 12-candle forward return and expose it to FreqUI/API charts.

    This strategy is intended for analysis and dry-run use. It does not place
    live orders; the included app starts Freqtrade with dry_run enabled.
    """

    INTERFACE_VERSION = 3

    timeframe = "15m"
    startup_candle_count = 200
    process_only_new_candles = True
    can_short = False
    minimal_roi = {"0": 0.05, "120": 0.0}
    stoploss = -0.03
    use_custom_stoploss = True
    use_exit_signal = True
    entry_threshold = DecimalParameter(
        0.003, 0.02, decimals=3, default=0.005, space="buy", optimize=False
    )

    plot_config = {
        "main_plot": {
            "ema_fast": {"color": "#f2b84b"},
            "ema_slow": {"color": "#54b5d8"},
            "forecast_price": {"color": "#a78bfa"},
        },
        "subplots": {
            "Tredink forecast (%)": {
                "forecast_return_pct": {"color": "#a78bfa"},
            },
            "Model data quality": {
                "do_predict": {"color": "#53d6a1"},
            },
            "RSI": {"rsi": {"color": "#f2b84b"}},
        },
    }

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: dict, **kwargs
    ) -> DataFrame:
        """Add features FreqAI expands across periods and configured timeframes."""
        dataframe[f"%-rsi-{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-adx-{period}"] = ta.ADX(dataframe, timeperiod=period)
        dataframe[f"%-mfi-{period}"] = ta.MFI(dataframe, timeperiod=period)
        dataframe[f"%-ema-distance-{period}"] = (
            ta.EMA(dataframe, timeperiod=period) / dataframe["close"] - 1
        )
        dataframe[f"%-atr-ratio-{period}"] = (
            ta.ATR(dataframe, timeperiod=period) / dataframe["close"]
        )

        bands = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2
        )
        dataframe[f"%-bb-width-{period}"] = (
            (bands["upper"] - bands["lower"]) / bands["mid"]
        )
        dataframe[f"%-close-to-bb-low-{period}"] = (
            dataframe["close"] / bands["lower"] - 1
        )
        dataframe[f"%-roc-{period}"] = ta.ROC(dataframe, timeperiod=period)
        dataframe[f"%-volume-ratio-{period}"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Add basic causal price and volume features."""
        dataframe["%-return-1"] = dataframe["close"].pct_change()
        dataframe["%-return-4"] = dataframe["close"].pct_change(4)
        dataframe["%-volume-change"] = dataframe["volume"].pct_change()
        dataframe["%-raw-volume"] = dataframe["volume"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Add calendar features once, without looking beyond the current candle."""
        dataframe["%-hour-of-day"] = dataframe["date"].dt.hour
        dataframe["%-day-of-week"] = dataframe["date"].dt.dayofweek
        return dataframe

    def set_freqai_targets(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Label each candle with the close-to-close return 12 candles ahead."""
        horizon = self.freqai_info["feature_parameters"]["label_period_candles"]
        dataframe["&-forward_return"] = dataframe["close"].shift(-horizon) / dataframe["close"] - 1
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Run the model and prepare chart-friendly forecast columns."""
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr_pct"] = ta.ATR(dataframe, timeperiod=14) / dataframe["close"]

        dataframe = self.freqai.start(dataframe, metadata, self)

        prediction = dataframe.get("&-forward_return")
        if prediction is None:
            dataframe["forecast_return"] = 0.0
        else:
            dataframe["forecast_return"] = prediction.fillna(0.0)

        dataframe["forecast_return_pct"] = dataframe["forecast_return"] * 100
        dataframe["forecast_price"] = dataframe["close"] * (1 + dataframe["forecast_return"])
        dataframe["forecast_signal"] = 0
        valid = dataframe.get("do_predict", 0) == 1
        dataframe.loc[
            valid & (dataframe["forecast_return"] >= self.entry_threshold.value),
            "forecast_signal",
        ] = 1
        dataframe.loc[
            valid & (dataframe["forecast_return"] <= -self.entry_threshold.value),
            "forecast_signal",
        ] = -1
        return dataframe

    def custom_stoploss(
        self,
        pair: str,
        trade,
        current_time,
        current_rate: float,
        current_profit: float,
        after_fill: bool = False,
        **kwargs,
    ) -> float | None:
        """Keep the paper/backtest stop aligned with the ATR risk shown in Tredink."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty or current_rate <= 0:
            return None

        atr_fraction = float(dataframe.iloc[-1].get("atr_pct", 0.0) or 0.0)
        if not math.isfinite(atr_fraction) or atr_fraction <= 0:
            return None

        stop_fraction = min(0.03, max(0.0075, atr_fraction * 2))
        stop_price = current_rate * (1 - stop_fraction)
        return stoploss_from_absolute(
            stop_price,
            current_rate=current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Mark a long paper-trade signal when the forecast clears 0.5%."""
        conditions = (
            dataframe["do_predict"] == 1,
            dataframe["forecast_return"] >= self.entry_threshold.value,
            dataframe["volume"] > 0,
        )
        entry = conditions[0] & conditions[1] & conditions[2]
        dataframe.loc[entry, "enter_long"] = 1
        dataframe.loc[entry, "enter_tag"] = "tredink_forecast_up"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Close a long paper-trade signal if the forecast turns negative."""
        condition = (dataframe["do_predict"] == 1) & (dataframe["forecast_return"] < 0)
        dataframe.loc[condition, "exit_long"] = 1
        dataframe.loc[condition, "exit_tag"] = "tredink_forecast_down"
        return dataframe
