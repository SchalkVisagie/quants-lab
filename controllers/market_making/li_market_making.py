from decimal import Decimal
from typing import List, Optional
import numpy as np
import pandas as pd

import pandas_ta as ta  # noqa: F401
from pydantic import Field, field_validator
from pydantic_core.core_schema import ValidationInfo

from hummingbot.core.data_type.common import TradeType, PriceType
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig


class LiMarketMakingConfig(MarketMakingControllerConfigBase):
    """
    Configuration for Li et al.'s Market Making Strategy with Order Book Pressure and News Signal.
    
    Formula: p_{t+1}^{b|a} = p_{t}^{b|a} + (sign_OBP x μ + sign_ns x η) x TickSize
    """
    controller_name: str = "li_market_making"
    candles_config: List[CandlesConfig] = []
    
    # Order Book Pressure Parameters
    obp_levels: int = Field(
        default=5,
        json_schema_extra={
            "prompt": "Enter the number of order book levels to use for OBP calculation (l): ",
            "prompt_on_new": True, "is_updatable": True
        }
    )
    obp_history_snapshots: int = Field(
        default=10,
        json_schema_extra={
            "prompt": "Enter the number of historical order book snapshots to use (n): ",
            "prompt_on_new": True, "is_updatable": True
        }
    )
    mu_scaling_factor: float = Field(
        default=0.5,
        json_schema_extra={
            "prompt": "Enter the scaling factor μ for OBP signal (number of ticks): ",
            "prompt_on_new": True, "is_updatable": True
        }
    )
    
    # News Signal Parameters (eta implementation)
    eta_scaling_factor: float = Field(
        default=0.3,
        json_schema_extra={
            "prompt": "Enter the scaling factor η for news signal (number of ticks): ",
            "prompt_on_new": True, "is_updatable": True
        }
    )
    news_signal_port: Optional[float] = Field(
        default=0.0,
        json_schema_extra={
            "prompt": "Enter the news signal value (-1 to 1, 0 for neutral): ",
            "prompt_on_new": False, "is_updatable": True
        }
    )
    
    # Volatility Calculation Parameters
    volatility_window: int = Field(
        default=24,
        json_schema_extra={
            "prompt": "Enter the volatility calculation window in hours: ",
            "prompt_on_new": True, "is_updatable": True
        }
    )
    
    # Candles Configuration
    candles_connector: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the connector for candles data (leave empty for same as trading connector): ",
            "prompt_on_new": True
        }
    )
    candles_trading_pair: str = Field(
        default=None,
        json_schema_extra={
            "prompt": "Enter the trading pair for candles data (leave empty for same as trading pair): ",
            "prompt_on_new": True
        }
    )
    interval: str = Field(
        default="1h",
        json_schema_extra={
            "prompt": "Enter the candle interval for volatility calculation (e.g., 1h, 1d): ",
            "prompt_on_new": True
        }
    )
    
    # OBP Threshold for signal activation
    obp_threshold: float = Field(
        default=0.1,
        json_schema_extra={
            "prompt": "Enter the OBP threshold for signal activation (0.0-1.0): ",
            "prompt_on_new": True, "is_updatable": True
        }
    )

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


class LiMarketMakingController(MarketMakingControllerBase):
    """
    Li et al.'s Market Making Strategy Controller.
    
    Implements dynamic bid/ask adjustment based on:
    1. Order Book Pressure (OBP) 
    2. News Signal (configurable port for external signals)
    3. Volatility-based scaling
    
    Formula: p_{t+1}^{b|a} = p_{t}^{b|a} + (sign_OBP x μ + sign_ns x η) x TickSize
    """
    
    def __init__(self, config: LiMarketMakingConfig, *args, **kwargs):
        self.config = config
        self.max_records = max(config.volatility_window * 2, 100)  # Ensure enough data for volatility calc
        
        # Initialize candles config if not provided
        if len(self.config.candles_config) == 0:
            self.config.candles_config = [CandlesConfig(
                connector=config.candles_connector,
                trading_pair=config.candles_trading_pair,
                interval=config.interval,
                max_records=self.max_records
            )]
        
        super().__init__(config, *args, **kwargs)
        
        # Order book history for OBP calculation
        self.orderbook_history = []
        self.max_orderbook_history = config.obp_history_snapshots
        
        # Processed data storage
        self.processed_data = {
            "current_volatility": Decimal("0.01"),
            "obp_signal": 0.0,
            "news_signal": 0.0,
            "price_adjustment": Decimal("0")
        }

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        """Create executor config with Li et al. price adjustments."""
        trade_type = self.get_trade_type_from_level_id(level_id)
        
        # Apply price adjustment based on Li et al. formula
        price_adjustment = self.processed_data.get("price_adjustment", Decimal("0"))
        
        if trade_type == TradeType.BUY:
            # For buy orders, positive adjustment increases bid price (more aggressive)
            adjusted_price = price + price_adjustment
        else:
            # For sell orders, positive adjustment increases ask price (more aggressive)  
            adjusted_price = price + price_adjustment
        
        # Ensure price is positive
        adjusted_price = max(adjusted_price, price * Decimal("0.5"))
        
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=adjusted_price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    async def update_processed_data(self):
        """Update volatility, OBP, and calculate price adjustments."""
        try:
            # 1. Calculate volatility from candles
            await self._calculate_volatility()
            
            # 2. Update order book history and calculate OBP
            self._update_orderbook_history()
            obp_signal = self._calculate_obp_signal()
            
            # 3. Get news signal (from external port)
            news_signal = self._get_news_signal()
            
            # 4. Calculate price adjustment based on Li et al. formula
            price_adjustment = self._calculate_price_adjustment(obp_signal, news_signal)
            
            # Store processed data
            self.processed_data.update({
                "obp_signal": obp_signal,
                "news_signal": news_signal,
                "price_adjustment": price_adjustment
            })
            
        except Exception as e:
            self.logger().error(f"Error updating processed data: {e}")


    '''
    Helper functions
    '''
    async def _calculate_volatility(self):
        """Calculate daily volatility from candles data."""
        try:
            candles = self.market_data_provider.get_candles_df(
                connector_name=self.config.candles_connector,
                trading_pair=self.config.candles_trading_pair,
                interval=self.config.interval,
                max_records=self.max_records
            )
            
            if len(candles) < self.config.volatility_window:
                # Use default volatility if not enough data
                self.processed_data["current_volatility"] = Decimal("0.01")
                return
                
            # Calculate returns
            returns = candles["close"].pct_change().dropna()
            
            # Calculate rolling volatility (annualized)
            volatility = returns.rolling(window=self.config.volatility_window).std().iloc[-1]
            
            # Annualize based on interval
            if self.config.interval.endswith('h'):
                hours = int(self.config.interval[:-1])
                volatility *= np.sqrt(24 / hours * 365)
            elif self.config.interval.endswith('d'):
                days = int(self.config.interval[:-1]) 
                volatility *= np.sqrt(365 / days)
            else:  # Default to hourly
                volatility *= np.sqrt(24 * 365)
            
            self.processed_data["current_volatility"] = Decimal(str(max(volatility, 0.001)))
            
        except Exception as e:
            self.logger().warning(f"Error calculating volatility: {e}")
            self.processed_data["current_volatility"] = Decimal("0.01")

    def _update_orderbook_history(self):
        """Update order book history for OBP calculation."""
        try:
            # Get current order book
            order_book = self.market_data_provider.get_order_book(
                connector_name=self.config.connector_name,
                trading_pair=self.config.trading_pair
            )
            
            if order_book is None:
                self.logger().warning(f"No order book for {self.config.connector_name} {self.config.trading_pair} at {self.market_data_provider.time()}")
                return
                
            # Extract bid and ask sizes for specified levels
            bid_sizes = []
            ask_sizes = []
            
            bids = order_book.bid_entries()[:self.config.obp_levels]
            asks = order_book.ask_entries()[:self.config.obp_levels]
            
            for bid in bids:
                bid_sizes.append(float(bid.amount))
            for ask in asks:
                ask_sizes.append(float(ask.amount))
                
            # Pad with zeros if not enough levels
            while len(bid_sizes) < self.config.obp_levels:
                bid_sizes.append(0.0)
            while len(ask_sizes) < self.config.obp_levels:
                ask_sizes.append(0.0)
                
            # Store snapshot
            snapshot = {
                'timestamp': self.market_data_provider.time(),
                'bid_sizes': bid_sizes,
                'ask_sizes': ask_sizes
            }
            
            self.orderbook_history.append(snapshot)
            
            # Maintain history size
            if len(self.orderbook_history) > self.max_orderbook_history:
                self.orderbook_history.pop(0)
                
        except Exception as e:
            self.logger().warning(f"Error updating order book history: {e}")

    def _calculate_obp_signal(self) -> float:
        """
        Calculate Order Book Pressure (OBP) signal.
        
        OBP(n, l) = Σ(τ=0 to n)Σ(j=1 to l)BidSize_j,t-τ / Σ(τ=0 to n)Σ(i=1 to l)AskSize_i,t-τ
        """
        if len(self.orderbook_history) == 0:
            return 0.0
            
        try:
            total_bid_volume = 0.0
            total_ask_volume = 0.0
            
            # Sum over history snapshots (τ) and levels (j/i)
            for snapshot in self.orderbook_history:
                total_bid_volume += sum(snapshot['bid_sizes'])
                total_ask_volume += sum(snapshot['ask_sizes'])
            
            if total_ask_volume == 0:
                return 1.0  # Maximum buy pressure
                
            obp = total_bid_volume / total_ask_volume
            
            # Convert to signal (-1 to 1)
            if obp > 1 + self.config.obp_threshold:
                return 1.0  # Strong buy pressure
            elif obp < 1 - self.config.obp_threshold:
                return -1.0  # Strong sell pressure
            else:
                return 0.0  # Neutral
                
        except Exception as e:
            self.logger().warning(f"Error calculating OBP signal: {e}")
            return 0.0

    def _get_news_signal(self) -> float:
        """Get news signal from external port (configurable)."""
        # This is the port where external news signals can be injected
        signal = self.config.news_signal_port or 0.0
        return max(-1.0, min(1.0, signal))  # Clamp to [-1, 1]

    def get_price_by_type(self, connector_name: str, trading_pair: str, price_type: PriceType):
        connector = self.get_connector(connector_name)
        if connector is None:
            self.logger().warning(f"No connector found for {connector_name}")
            return None
        price = connector.get_price_by_type(trading_pair, price_type)
        if price is None:
            self.logger().warning(f"No price found for {trading_pair} ({price_type}) on {connector_name}")
        return price

    def get_mid_price(self, connector_name: str, trading_pair: str) -> Decimal:
        """
        Returns the mid price for the specified connector and trading pair.
        """
        return self.get_price_by_type(connector_name, trading_pair, PriceType.MidPrice)
    

    def _calculate_price_adjustment(self, obp_signal: float, news_signal: float) -> Decimal:
        """
        Calculate price adjustment based on Li et al. formula:
        Adjustment = (sign_OBP x μ + sign_ns x η) x TickSize x σ x √(T_OBP/T_n)
        """
        try:
            # Get tick size (approximate from current price)
            print(f"{self.config.connector_name}, {self.config.trading_pair}")
            current_price = self.get_mid_price(self.config.connector_name, self.config.trading_pair)
            print(f"Current price: {current_price}")
            tick_size = current_price * Decimal("0.0001")  # Approximate tick size
            
            # Calculate volatility scaling for η
            volatility = self.processed_data["current_volatility"]
            time_scaling = np.sqrt(1.0)  # Simplified time scaling
            
            # Calculate adjustment components
            obp_component = obp_signal * self.config.mu_scaling_factor
            news_component = news_signal * self.config.eta_scaling_factor * float(volatility) * time_scaling
            
            # Total adjustment
            total_adjustment = (obp_component + news_component) * float(tick_size)
            
            return Decimal(str(total_adjustment))
            
        except Exception as e:
            self.logger().warning(f"Error calculating price adjustment: {e}")
            return Decimal("0")


    def get_processed_data_dict(self) -> dict:
        """Return processed data for analysis."""
        return {
            "volatility": float(self.processed_data["current_volatility"]),
            "obp_signal": self.processed_data["obp_signal"],
            "news_signal": self.processed_data["news_signal"],
            "price_adjustment": float(self.processed_data["price_adjustment"]),
            "orderbook_history_length": len(self.orderbook_history)
        }