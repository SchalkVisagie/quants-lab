from decimal import Decimal
from typing import List, Tuple
import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401

from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig

"""
Summary of Implemented Functionality:

1) Initialization: The controller now starts with current_buy_prices and current_sell_prices set to the initial values from your configuration.

2) Per-Tick Adjustment: In update_processed_data:
obp_sign is calculated.
news_signal_value is taken from the config.
The price_adjustment_per_tick is computed using the formula and configured mu and eta scaling factors.
This adjustment is applied to each price in current_buy_prices and current_sell_prices, and the results are quantized.

3) Order Placement: get_price_and_amount now uses these dynamically adjusted current_buy_prices and current_sell_prices to determine the order_price. The order amount calculation remains based on the total_amount_quote and percentage distributions defined in the config.
4) Configuration: New fields (mu_scaling_factor, eta_scaling_factor, news_signal_value, natr_length) are added to PMMLiControllerConfig for better control.
5) Logging/Status: processed_data is updated with relevant information like the OBP sign, news signal, the adjustment applied, and the current adjusted prices for monitoring.
6) Compatibility:
The reference_price method is maintained to provide a reasonable mid-price for balance requirement calculations in the base class.
get_not_active_levels_ids is overridden to correctly identify missing levels based on buy_prices/sell_prices length.
"""


class PMMLiControllerConfig(MarketMakingControllerConfigBase):
    controller_name: str = "pmm_li"
    candles_config: List[CandlesConfig] = []
    buy_prices: List[Decimal] = Field(
        json_schema_extra={
            "prompt": "Enter a comma-separated list of absolute buy prices (e.g., '99.8,99.5'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    sell_prices: List[Decimal] = Field(
        json_schema_extra={
            "prompt": "Enter a comma-separated list of absolute sell prices (e.g., '100.2,100.5'): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    # buy_amounts_pct and sell_amounts_pct are inherited. We will override their validator.

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
        default="1m",
        json_schema_extra={
            "prompt": "Enter the candle interval (e.g., 1m, 5m, 1h, 1d): ",
            "prompt_on_new": True})
    orderbook_window: int = Field(
        default=5,
        json_schema_extra={"prompt": "Enter the lookback period for calculating orderbook pressure: ", "prompt_on_new": True})
    natr_length: int = Field(
        default=14,
        json_schema_extra={"prompt": "Enter the NATR length for volatility calculation (used for logging): ", "prompt_on_new": True}
    )
    mu_scaling_factor: float = Field(
        default=1.0,
        json_schema_extra={
            "prompt": "Enter the scaling factor μ for OBP signal (determines number of ticks for adjustment): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    eta_scaling_factor: float = Field(
        default=0.5,
        json_schema_extra={
            "prompt": "Enter the scaling factor η for news signal (determines number of ticks for adjustment): ",
            "prompt_on_new": True, "is_updatable": True}
    )
    news_signal_value: float = Field(
        default=0.0,
        ge=-1.0, le=1.0,
        json_schema_extra={
            "prompt": "Enter the news signal value (-1.0 to 1.0): ",
            "prompt_on_new": True, "is_updatable": True}
    )

    @field_validator("buy_prices", "sell_prices", mode="before")
    @classmethod
    def parse_prices_list(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return []
            try:
                return [Decimal(x.strip()) for x in v.split(',')]
            except Exception as e:
                raise ValueError(f"Invalid price list format: {v}. Error: {e}")
        elif isinstance(v, list):
            try:
                return [Decimal(str(x)) for x in v]
            except Exception as e:
                raise ValueError(f"Invalid price list format: {v}. Error: {e}")
        raise ValueError(f"Invalid type for price list: {type(v)}. Expected str or list.")

    @field_validator('buy_amounts_pct', 'sell_amounts_pct', mode="before")
    @classmethod
    def parse_and_validate_amounts(cls, v, validation_info: ValidationInfo):
        field_name = validation_info.field_name
        prices_field_name = "buy_prices" if "buy" in field_name else "sell_prices"
        
        prices_data = validation_info.data.get(prices_field_name)
        num_levels = len(prices_data) if prices_data is not None else 0

        if v is None or v == "":
            if num_levels == 0: # If no prices are defined, no amounts can be defaulted.
                return [] 
            return [Decimal("1.0") for _ in range(num_levels)] # Default to equal distribution

        if isinstance(v, str):
            try:
                parsed_amounts = [Decimal(x.strip()) for x in v.split(',')]
            except Exception as e:
                raise ValueError(f"Invalid amounts_pct format for {field_name}: {v}. Error: {e}")
        elif isinstance(v, list):
            try:
                parsed_amounts = [Decimal(str(x)) for x in v]
            except Exception as e:
                raise ValueError(f"Invalid amounts_pct format for {field_name}: {v}. Error: {e}")
        else:
            raise ValueError(f"Invalid type for {field_name}: {type(v)}. Expected str or list.")

        if num_levels > 0 and len(parsed_amounts) != num_levels:
            # This check should ideally happen after prices_field_name is fully validated and in validation_info.data
            # Pydantic's behavior should ensure this if fields are ordered correctly or dependencies handled.
            if prices_data is not None and len(parsed_amounts) != len(prices_data):
                raise ValueError(
                    f"The number of {field_name} ({len(parsed_amounts)}) must match the number of {prices_field_name} ({len(prices_data)})."
                )
        return parsed_amounts

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


class PMMLiController(MarketMakingControllerBase):
    """
    Pmm strat like Li et. al. proposes in their paper. It also uses the Triple Barrier Strategy to manage the risk.
    """
    def __init__(self, config: PMMLiControllerConfig, *args, **kwargs):
        self.config = config
        self.max_records = max(config.orderbook_window, config.natr_length) + 100  # Ensure enough data for indicators
        if len(self.config.candles_config) == 0:
            interval = config.interval[0] if isinstance(config.interval, tuple) else config.interval
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=interval,
                max_records=self.max_records
            )]
        super().__init__(config, *args, **kwargs)
        # Initialize current prices with configured initial prices
        self.current_buy_prices: List[Decimal] = [p for p in self.config.buy_prices]
        self.current_sell_prices: List[Decimal] = [p for p in self.config.sell_prices]

    async def update_processed_data(self):
        """
        Updates OBP, news signal, and then adjusts each order level's price based on the formula:
        p_{t+1}^{b|a} = p_{t}^{b|a} + (sign_{OBP} \times \mu + sign_{ns} \times \eta) \times TickSize
        """
        candles = self.market_data_provider.get_candles_df(connector_name=self.config.candles_connector,
                                                           trading_pair=self.config.candles_trading_pair,
                                                           interval=self.config.interval,
                                                           max_records=self.max_records)

        obp_sign = self.compute_obp_sign(candles, n=self.config.orderbook_window)
        news_signal = self.config.news_signal_value  # sign_ns

        trading_rules = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
        tick_size = trading_rules.min_price_increment

        # Calculate the adjustment in number of ticks, then multiply by tick_size
        adjustment_in_ticks = Decimal(str(obp_sign * self.config.mu_scaling_factor + news_signal * self.config.eta_scaling_factor))
        price_adjustment_per_tick = adjustment_in_ticks * tick_size

        next_buy_prices = []
        for p_t in self.current_buy_prices:
            p_next = p_t + price_adjustment_per_tick
            p_next_quantized = self.market_data_provider.quantize_order_price(self.config.trading_pair, p_next)
            next_buy_prices.append(max(tick_size, p_next_quantized))  # Ensure price is positive
        self.current_buy_prices = next_buy_prices

        next_sell_prices = []
        for p_t in self.current_sell_prices:
            p_next = p_t + price_adjustment_per_tick
            p_next_quantized = self.market_data_provider.quantize_order_price(self.config.trading_pair, p_next)
            next_sell_prices.append(max(tick_size, p_next_quantized)) # Ensure price is positive
        self.current_sell_prices = next_sell_prices

        # For logging and balance requirements, calculate a reference price and spread multiplier
        natr = ta.natr(candles["high"], candles["low"], candles["close"], length=self.config.natr_length) / 100
        candles["spread_multiplier_log"] = natr # For logging
        
        # The reference_price method calculates an adjusted mid-price based on Li et al.
        # This is useful for self.processed_data["reference_price"] used by base class.
        adjusted_mid_price_series = self.reference_price(candles=candles)
        
        self.processed_data = {
            "reference_price": Decimal(adjusted_mid_price_series.iloc[-1]), # Used for balance requirements
            "spread_multiplier_log": Decimal(candles["spread_multiplier_log"].iloc[-1]),
            "obp_sign": obp_sign,
            "news_signal": news_signal,
            "price_adjustment_applied_per_tick": price_adjustment_per_tick,
            "current_adjusted_buy_prices": [str(p) for p in self.current_buy_prices],
            "current_adjusted_sell_prices": [str(p) for p in self.current_sell_prices],
            "features": candles
        }

    def get_spreads_and_amounts_in_quote(self, trade_type: TradeType) -> Tuple[List[Decimal], List[Decimal]]:
        """
        Returns the configured prices and calculated quote amounts for each order level.
        The first element of the tuple (prices) replaces the spreads from the base class method.
        """
        buy_amounts_pct = [Decimal(str(d)) for d in (self.buy_amounts_pct or [])]
        sell_amounts_pct = [Decimal(str(d)) for d in (self.sell_amounts_pct or [])]

        total_pct = sum(buy_amounts_pct) + sum(sell_amounts_pct)

        if total_pct == Decimal("0"):
            normalized_amounts_pct = [Decimal("0")] * (len(buy_amounts_pct) if trade_type == TradeType.BUY else len(sell_amounts_pct))
        else:
            current_side_amounts_pct = buy_amounts_pct if trade_type == TradeType.BUY else sell_amounts_pct
            normalized_amounts_pct = [amt_pct / total_pct for amt_pct in current_side_amounts_pct]

        amounts_quote = [norm_pct * self.total_amount_quote for norm_pct in normalized_amounts_pct]

        if trade_type == TradeType.BUY:
            prices_for_side = self.buy_prices
        else:  # TradeType.SELL
            prices_for_side = self.config.sell_prices # Corrected: was self.sell_prices, should be self.config.sell_prices
        
        return prices_for_side, amounts_quote

    def get_price_and_amount(self, level_id: str) -> Tuple[Decimal, Decimal]:
        """
        Get the dynamically adjusted price and calculated base amount for a given level_id.
        """
        level = self.get_level_from_level_id(level_id)
        trade_type = self.get_trade_type_from_level_id(level_id)

        if trade_type == TradeType.BUY:
            if level >= len(self.current_buy_prices):
                raise IndexError(f"Buy level {level} is out of bounds for current_buy_prices (len: {len(self.current_buy_prices)})")
            order_price = self.current_buy_prices[level]
        else:  # TradeType.SELL
            if level >= len(self.current_sell_prices):
                raise IndexError(f"Sell level {level} is out of bounds for current_sell_prices (len: {len(self.current_sell_prices)})")
            order_price = self.current_sell_prices[level]

        # Fetch quote amounts based on initial config percentages.
        # self.config.get_spreads_and_amounts_in_quote returns (initial_prices_list, amounts_quote_list)
        # We only need the amounts_quote_list here.
        _initial_prices_for_side, amounts_quote_for_side = self.config.get_spreads_and_amounts_in_quote(trade_type)
        
        if level >= len(amounts_quote_for_side):
             raise IndexError(f"Level {level} is out of bounds for amounts_quote_for_side (len: {len(amounts_quote_for_side)}) for {trade_type}")
        amount_quote_for_level = amounts_quote_for_side[level]

        if order_price <= Decimal("0"):
            raise ValueError(f"Order price must be positive. Got {order_price} for level_id={level_id}, level {level}")
        
        order_amount_base = amount_quote_for_level / order_price
        
        # Quantize the base amount
        quantized_amount_base = self.market_data_provider.quantize_order_amount(
            trading_pair=self.config.trading_pair,
            amount=order_amount_base,
            price=order_price # Some exchanges might need price for amount quantization (e.g. for quote value)
        )
        return order_price, quantized_amount_base

    def compute_obp_sign(self, candles: pd.DataFrame, n: int = 5) -> int:
        """
        Computes the sign of Order Book Pressure using proxy volumes from candles.
        n: number of candles to consider
        Returns:
            +1 if buy pressure > sell pressure
            -1 if sell pressure > buy pressure
            0 otherwise
        """
        buy_pressure = candles['taker_buy_base_volume'].tail(n).sum()
        sell_pressure = (candles['volume'] - candles['taker_buy_base_volume']).tail(n).sum()

        if buy_pressure > sell_pressure:
            return 1
        elif sell_pressure > buy_pressure:
            return -1
        else:
            return 0
    
    def reference_price(self, candles: pd.DataFrame) -> float:
        """
        Calculates an adjusted mid-price based on OBP and news signal, similar to Li et al.
        This is primarily used to set self.processed_data["reference_price"] for balance calculations.
        The actual order prices are set via self.current_buy_prices and self.current_sell_prices.
        """
        mid_price = candles["close"]

        trading_rules = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
        tick_size = float(trading_rules.min_price_increment)
        
        # Use configured factors for consistency if this reference price needs to be very precise
        # For simplicity, using the same obp_sign as calculated in update_processed_data
        obp_sign = self.compute_obp_sign(candles, n=self.config.orderbook_window)
        news_signal = self.config.news_signal_value

        # These mu and eta are for the reference mid-price adjustment, could be different from order level adjustment factors
        ref_mu = self.config.mu_scaling_factor
        ref_eta = self.config.eta_scaling_factor

        offset_ticks = (obp_sign * ref_mu + news_signal * ref_eta)
        adjusted_price = mid_price + offset_ticks * tick_size

        return adjusted_price

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price, # This price comes from the overridden get_price_and_amount
            amount=amount,     # This amount also comes from the overridden get_price_and_amount
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    def get_not_active_levels_ids(self, active_levels_ids: List[str]) -> List[str]:
        """
        Get the levels to execute based on the current state of the controller.
        Overrides base to use buy_prices/sell_prices from config instead of spreads.
        """
        buy_ids_missing = [self.get_level_id_from_side(TradeType.BUY, level) for level in range(len(self.config.buy_prices))
                           if self.get_level_id_from_side(TradeType.BUY, level) not in active_levels_ids]
        sell_ids_missing = [self.get_level_id_from_side(TradeType.SELL, level) for level in range(len(self.config.sell_prices))
                            if self.get_level_id_from_side(TradeType.SELL, level) not in active_levels_ids]
        return buy_ids_missing + sell_ids_missing

    # def spread_multiplier(self) -> float:
    #     obp_sign = self.compute_obp_sign(self.candles, n=5)
    #     # Make spread tighter when pressure is strong (increase fill rate)
    #     return 0.9 if obp_sign != 0 else 1.0
