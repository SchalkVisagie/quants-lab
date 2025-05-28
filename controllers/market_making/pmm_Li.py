from decimal import Decimal
from typing import List
import pandas as pd
import asyncio
import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

# from controllers.market_making.pmm_Li import PMMLIController, PMMLIControllerConfig
from hummingbot.strategy_v2.executors.position_executor.data_types import TrailingStop
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig


class PMMLIControllerConfig(MarketMakingControllerConfigBase):
    """
    Configuration for Li et al.'s Market Making Strategy with Order Book Pressure and News Signal.
    
    Formula: p_{t+1}^{b|a} = p_{t}^{b|a} + (sign_OBP x μ + sign_ns x η) x TickSize
    """

    controller_name: str = "pmm_dynamic"
    candles_config: List[CandlesConfig] = []
    buy_spreads: List[float] = Field(
        default="1,2,4",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of buy spreads measured in units of volatility(e.g., '1, 2'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_spreads: List[float] = Field(
        default="1,2,4",
        json_schema_extra={
            "prompt": "Enter a comma-separated list of sell spreads measured in units of volatility(e.g., '1, 2'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    volatility_window: int = Field(
        default=86400,
        json_schema_extra={"prompt": "Enter the volatility window size (default is 24h): ", "prompt_on_new": True}
    )
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for the candles data, leave empty to use the same exchange as the connector: ",
            "prompt_on_new": True})
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for the candles data, leave empty to use the same trading pair as the connector: ",
            "prompt_on_new": True})
    interval: str = Field(
        default="3m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True})
    macd_fast: int = Field(
        default=21,
        json_schema_extra={"prompt": "Enter the MACD fast period: ", "prompt_on_new": True})
    macd_slow: int = Field(
        default=42,
        json_schema_extra={"prompt": "Enter the MACD slow period: ", "prompt_on_new": True})
    macd_signal: int = Field(
        default=9,
        json_schema_extra={"prompt": "Enter the MACD signal period: ", "prompt_on_new": True})
    natr_length: int = Field(
        default=14,
        json_schema_extra={"prompt": "Enter the NATR length: ", "prompt_on_new": True})

    @field_validator("candles_connector", mode="before")
    @classmethod
    def set_candles_connector(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("connector_name")
        return v

    @field_validator("candles_trading_pair", mode="before")
    @classmethod
    def set_candles_trading_pair(cls, v, validation_info: ValidationInfo):
        if v is None or v == "":
            return validation_info.data.get("trading_pair")
        return v


class PMMLIController(MarketMakingControllerBase):

    """
    This is a dynamic version of the PMM controller.It uses the MACD to shift the mid-price and the NATR
    to make the spreads dynamic. It also uses the Triple Barrier Strategy to manage the risk.
    """
    def __init__(self, config: PMMLIControllerConfig, *args, **kwargs):
        self.config = config
        self.obp = 0 # Placeholder for Order Book Pressure

        self.max_records = max(config.volatility_window * 2, 100)  # Ensure enough data for volatility calc
        
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=config.interval,
                max_records=self.max_records
            )]
        super().__init__(config, *args, **kwargs)

    async def update_processed_data(self):
        candles = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                           trading_pair=self.config.candles_trading_pair,
                                                           interval=self.config.interval,
                                                           max_records=self.max_records)
        
        print(f"Processing {len(candles)} and {candles.columns}")

        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        macd_output = ta.macd(candles["close"], fast=self.config.macd_fast,
                              slow=self.config.macd_slow, signal=self.config.macd_signal)
        macd = macd_output[f"MACD_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]
        macd_signal = - (macd - macd.mean()) / macd.std()
        macdh = macd_output[f"MACDh_{self.config.macd_fast}_{self.config.macd_slow}_{self.config.macd_signal}"]
        macdh_signal = macdh.apply(lambda x: 1 if x > 0 else -1)
        max_price_shift = natr / 2
        price_multiplier = ((0.5 * macd_signal + 0.5 * macdh_signal) * max_price_shift).iloc[-1]

        
        
        candles["spread_multiplier"] = natr
        candles["reference_price"] = candles["close"] * (1 + price_multiplier)
        
        self.processed_data = {
            "reference_price": Decimal(candles["reference_price"].iloc[-1]),
            "spread_multiplier": Decimal(candles["spread_multiplier"].iloc[-1]),
            "features": candles
        }

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )
    
    def computeOBP(quote_asset_volume: float, taker_buy_base_volume: float) -> float:
        """
        Computes the Orderbook Pressure (OBP) based on the total volume and taker buy volume.
        TODO: Implement as Li et al. (2023) suggests later on.
        OBP = (taker_buy_base_volume - taker_sell_base_volume) / (taker_buy_base_volume + taker_sell_base_volume)
        :param quote_asset_volume: Total volume of trades (base asset).
        :param taker_buy_base_volume: Volume of taker buy trades (base asset).
        :return: Order book pressure as a float in [-1, 1].
        """
        taker_sell_base_volume = quote_asset_volume - taker_buy_base_volume
        denominator = taker_buy_base_volume + taker_sell_base_volume
        if denominator == 0:
            return 0.0
        obp = (taker_buy_base_volume - taker_sell_base_volume) / denominator
        return obp
    

# Dummy market data provider for testing
class DummyMarketDataProvider:
    def get_candles_df(self, connector_name, trading_pair, interval, max_records):
        # Create a dummy DataFrame with the required columns
        data = {
            "timestamp": pd.date_range("2025-05-25", periods=max_records, freq="min"),
            "open": [1.0] * max_records,
            "high": [1.1] * max_records,
            "low": [0.9] * max_records,
            "close": [1.0 + 0.001 * i for i in range(max_records)],
            "volume": [100] * max_records,
            "quote_asset_volume": [1000] * max_records,
            "n_trades": [10] * max_records,
            "taker_buy_base_volume": [50] * max_records,
            "taker_buy_quote_volume": [500] * max_records,
        }
        return pd.DataFrame(data)
    def time(self):
        import time
        return time.time()

def main():
    # Config values as in your notebook
    config = PMMLIControllerConfig(
        connector_name="binance",
        trading_pair="POL-USDT",
        candles_connector="binance",
        candles_trading_pair="POL-USDT",
        volatility_window=60 * 60 * 24,
        total_amount_quote=Decimal("1000"),
        take_profit=Decimal("0.02"),
        stop_loss=Decimal("0.01"),
        trailing_stop=TrailingStop(
            activation_price=Decimal("0.015"),
            trailing_delta=Decimal("0.07")
        ),
        time_limit=60 * 60 * 2,
        executor_refresh_time=60 * 6,
        cooldown_time=600,
        buy_spreads=[0.5, 1.0, 1.5, 2.0],
        sell_spreads=[0.5, 1.0, 1.5, 2.0],
        buy_amounts_pct=[Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4")],
        sell_amounts_pct=[Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4")],
    )

    controller = PMMLIController(config)
    controller.market_data_provider = DummyMarketDataProvider()

    async def test_update():
        await controller.update_processed_data()
        print("Processed data:", controller.processed_data)
        # Test computeOBP
        obp = controller.computeOBP(quote_asset_volume=1000, taker_buy_base_volume=600)
        print("OBP(1000, 600):", obp)

    asyncio.run(test_update())

if __name__ == "__main__":
    main()