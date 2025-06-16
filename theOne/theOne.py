from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Optional, Any
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
import logging

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"

class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"

@dataclass
class Order:
    """Represents a single limit order"""
    price: float
    quantity: float
    side: OrderSide
    level: int
    timestamp: float
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    order_id: str = field(default_factory=lambda: f"order_{np.random.randint(1e6, 1e7)}")

@dataclass
class Fill:
    """Represents an order fill event"""
    order_id: str
    price: float
    quantity: float
    side: OrderSide
    timestamp: float
    level: int

@dataclass
class MarketData:
    """Container for market data at a single timestamp"""
    timestamp: float
    bids: List[Tuple[float, float]]  # [(price, quantity), ...]
    asks: List[Tuple[float, float]]
    taker_buy_volume: float
    taker_sell_volume: float
    close_price: float

@dataclass
class Portfolio:
    """Portfolio state tracking"""
    base_balance: float = 0.0
    quote_balance: float = 0.0
    base_cost_basis: float = 0.0
    realized_pnl: float = 0.0
    
    def update_fill(self, fill: Fill):
        """Update portfolio based on a fill - FIXED"""
        if fill.side == OrderSide.BUY:
            cost = fill.price * fill.quantity
            self.quote_balance -= cost
            
            # Fixed cost basis calculation to match original
            if self.base_balance > 0:
                total_cost = (self.base_balance * self.base_cost_basis) + cost
                self.base_balance += fill.quantity
                self.base_cost_basis = total_cost / self.base_balance
            else:
                self.base_balance = fill.quantity
                self.base_cost_basis = fill.price
                
        else:  # SELL
            proceeds = fill.price * fill.quantity
            self.quote_balance += proceeds
            
            # Match original realized PnL calculation exactly
            if self.base_balance > 0:
                avg_cost = self.base_cost_basis * fill.quantity
                self.realized_pnl += proceeds - avg_cost
                self.base_balance -= fill.quantity
                
                # Prevent negative base balance (no short selling in original)
                if self.base_balance < 0:
                    self.base_balance = 0
    
    def get_unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized PnL based on current market price"""
        if self.base_balance <= 0:
            return 0.0
        market_value = self.base_balance * current_price
        cost_value = self.base_balance * self.base_cost_basis
        return market_value - cost_value
    
    def get_total_value(self, current_price: float) -> float:
        """Get total portfolio value in quote currency"""
        return self.quote_balance + (self.base_balance * current_price)

class MarketMakingStrategy(ABC):
    """Abstract base class for market making strategies"""
    
    def __init__(self, name: str = "BaseStrategy"):
        self.name = name
        self.logger = logging.getLogger(f"Strategy.{name}")
    
    @abstractmethod
    def calculate_quotes(self, market_data: MarketData, portfolio: Portfolio, 
                        historical_data: List[MarketData]) -> Tuple[List[Order], List[Order]]:
        """
        Calculate bid and ask orders based on current market conditions.
        
        Returns:
            Tuple of (bid_orders, ask_orders) where each is a list of Order objects
        """
        pass
    
    def should_refresh_orders(self, current_time: float, last_refresh: float, 
                            market_data: MarketData) -> bool:
        """
        Determine if orders should be refreshed.
        Override for custom refresh logic.
        """
        return True

class LiEtAlStrategy(MarketMakingStrategy):
    """Implementation of Li et al. market making strategy"""
    
    def __init__(self, tick_size: float = 0.0001, mu: float = 2.0, 
                 spread: float = 0.0005, refresh_interval: int = 5,
                 n_candles: int = 5, orderbook_levels: int = 5,
                 order_levels: int = 3, base_quantity: float = 100.0):
        super().__init__("LiEtAl")
        self.tick_size = tick_size
        self.mu = mu
        self.spread = spread
        self.refresh_interval = refresh_interval
        self.n_candles = n_candles
        self.orderbook_levels = orderbook_levels
        self.order_levels = order_levels
        self.base_quantity = base_quantity
        self.last_refresh = -self.refresh_interval
    
    def compute_obp(self, historical_data: List[MarketData]) -> float:
        """Compute Order Book Pressure"""
        if len(historical_data) < self.n_candles:
            return 0
        
        recent_data = historical_data[-self.n_candles:]
        bid_sum = 0
        ask_sum = 0
        
        for data in recent_data:
            bids = data.bids[:self.orderbook_levels]
            asks = data.asks[:self.orderbook_levels]
            bid_sum += sum(qty for price, qty in bids)
            ask_sum += sum(qty for price, qty in asks)
        
        if ask_sum == 0 and bid_sum == 0:
            return 0
        if ask_sum == 0:
            return 1
        
        return bid_sum / ask_sum
    
    def should_refresh_orders(self, current_time: float, last_refresh: float, 
                            market_data: MarketData) -> bool:
        """Refresh orders every refresh_interval seconds"""
        return (current_time - last_refresh) >= self.refresh_interval
    
    def calculate_quotes(self, market_data: MarketData, portfolio: Portfolio, 
                        historical_data: List[MarketData]) -> Tuple[List[Order], List[Order]]:
        """Calculate bid and ask orders using Li et al. methodology"""
        
        # Calculate OBP and skew
        obp = self.compute_obp(historical_data)
        obp_sign = 1 if obp > 1 else (-1 if obp < 1 else 0)
        
        # Get mid price
        best_bid = market_data.bids[0][0] if market_data.bids else 0
        best_ask = market_data.asks[0][0] if market_data.asks else 0
        mid_price = (best_bid + best_ask) / 2
        
        # Calculate skewed mid price
        adjustment = obp_sign * self.mu * self.tick_size
        skewed_mid_price = mid_price + adjustment
        
        # Generate bid orders
        bid_base_price = skewed_mid_price - (self.spread / 2)
        bid_orders = []
        for level in range(self.order_levels):
            price = bid_base_price - (level * self.tick_size)
            quantity = self.base_quantity * (2 ** level)  # 100, 200, 400
            bid_orders.append(Order(
                price=price,
                quantity=quantity,
                side=OrderSide.BUY,
                level=level + 1,
                timestamp=market_data.timestamp
            ))
        
        # Generate ask orders
        ask_base_price = skewed_mid_price + (self.spread / 2)
        ask_orders = []
        for level in range(self.order_levels):
            price = ask_base_price + (level * self.tick_size)
            quantity = self.base_quantity * (2 ** level)  # 100, 200, 400
            ask_orders.append(Order(
                price=price,
                quantity=quantity,
                side=OrderSide.SELL,
                level=level + 1,
                timestamp=market_data.timestamp
            ))
        
        return bid_orders, ask_orders

class FillSimulator:
    """Simulates realistic order fills based on market volume changes"""
    
    @staticmethod
    def simulate_fills(outstanding_orders: List[Order], market_data: MarketData, 
                      prev_market_data: MarketData) -> List[Fill]:
        """
        Simulate order fills based on volume changes and FIFO queue logic
        """
        fills = []
        
        # Calculate volume changes
        buy_volume_change = market_data.taker_buy_volume - prev_market_data.taker_buy_volume
        sell_volume_change = market_data.taker_sell_volume - prev_market_data.taker_sell_volume
        
        # Process bid orders (filled by market sells)
        if sell_volume_change > 0:
            bid_orders = [o for o in outstanding_orders if o.side == OrderSide.BUY and o.status == OrderStatus.PENDING]
            bid_orders.sort(key=lambda x: x.price, reverse=True)  # Highest price first
            
            for order in bid_orders:
                fill_qty = FillSimulator._calculate_bid_fill(
                    order, market_data, sell_volume_change
                )
                if fill_qty > 0:
                    fills.append(Fill(
                        order_id=order.order_id,
                        price=order.price,
                        quantity=fill_qty,
                        side=order.side,
                        timestamp=market_data.timestamp,
                        level=order.level
                    ))
                    order.filled_quantity += fill_qty
                    if order.filled_quantity >= order.quantity:
                        order.status = OrderStatus.FILLED
        
        # Process ask orders (filled by market buys)
        if buy_volume_change > 0:
            ask_orders = [o for o in outstanding_orders if o.side == OrderSide.SELL and o.status == OrderStatus.PENDING]
            ask_orders.sort(key=lambda x: x.price)  # Lowest price first
            
            for order in ask_orders:
                fill_qty = FillSimulator._calculate_ask_fill(
                    order, market_data, buy_volume_change
                )
                if fill_qty > 0:
                    fills.append(Fill(
                        order_id=order.order_id,
                        price=order.price,
                        quantity=fill_qty,
                        side=order.side,
                        timestamp=market_data.timestamp,
                        level=order.level
                    ))
                    order.filled_quantity += fill_qty
                    if order.filled_quantity >= order.quantity:
                        order.status = OrderStatus.FILLED
        
        return fills
    
    @staticmethod
    def _calculate_bid_fill(order: Order, market_data: MarketData, sell_volume: float) -> float:
        """Calculate how much of a bid order gets filled - FIXED to match original"""
        # Volume ahead of this order in the queue
        volume_ahead = sum(qty for price, qty in market_data.bids if price > order.price)
        volume_at_same_price = sum(qty for price, qty in market_data.bids if price == order.price)
        
        total_volume_before = volume_ahead + volume_at_same_price
        
        # Original: if sell_volume > total_volume_before, we get filled
        if sell_volume <= total_volume_before:
            return 0.0
        
        # Available volume for our order (simplified like original)
        available_volume = sell_volume - total_volume_before
        remaining_order_qty = order.quantity - order.filled_quantity
        
        return min(remaining_order_qty, available_volume)
    
    @staticmethod
    def _calculate_ask_fill(order: Order, market_data: MarketData, buy_volume: float) -> float:
        """Calculate how much of an ask order gets filled"""
        # Volume ahead of this order in the queue
        volume_ahead = sum(qty for price, qty in market_data.asks if price < order.price)
        volume_at_same_price = sum(qty for price, qty in market_data.asks if price == order.price)
        
        # Conservative approach: assume market orders at same price have priority
        if buy_volume <= volume_ahead:
            return 0.0
        
        remaining_volume = buy_volume - volume_ahead
        if remaining_volume <= volume_at_same_price:
            return 0.0  # Market orders at same price fill first
        
        # Volume available for our order
        available_volume = remaining_volume - volume_at_same_price
        remaining_order_qty = order.quantity - order.filled_quantity
        
        return min(remaining_order_qty, available_volume)

class MarketMakingBacktester:
    """Main backtesting engine for market making strategies"""
    
    def __init__(self, strategy: MarketMakingStrategy, initial_base: float = 1000.0, 
                 initial_quote: float = 2000.0, initial_cost_basis: float = 0.262):
        self.strategy = strategy
        self.portfolio = Portfolio(
            base_balance=initial_base,
            quote_balance=initial_quote,
            base_cost_basis=initial_cost_basis
        )
        self.outstanding_orders: List[Order] = []
        self.fills: List[Fill] = []
        self.historical_data: List[MarketData] = []
        self.performance_metrics: List[Dict] = []
        self.last_refresh_time = 0.0
        self.last_refresh_idx = -strategy.refresh_interval
        # Setup logging
        self.logger = logging.getLogger("MarketMakingBacktester")
    
    def run_backtest(self, market_data_df: pd.DataFrame) -> Dict[str, Any]:
        """Run backtest - FIXED to match original sequence"""
        self.logger.info(f"Starting backtest with {len(market_data_df)} data points")
        
        for idx in range(len(market_data_df) - 1):
            current_row = market_data_df.iloc[idx]
            next_row = market_data_df.iloc[idx + 1]
            
            current_market = self._row_to_market_data(current_row)
            next_market = self._row_to_market_data(next_row)
            
            self.historical_data.append(current_market)
            
            # CRITICAL: Use idx-based logic to match original exactly
            if (idx - self.last_refresh_idx) >= self.strategy.refresh_interval:
                self._refresh_orders(current_market)
                self.last_refresh_idx = idx  # Use index, not timestamp
            
            # Check for volume changes (match original logic exactly)
            buy_vol_changed = current_row['taker_buy_base_volume'] != next_row['taker_buy_base_volume']
            sell_vol_changed = current_row['taker_sell_base_volume'] != next_row['taker_sell_base_volume']
            
            # Only simulate fills if volume actually changed (matches original)
            if buy_vol_changed or sell_vol_changed:
                fills = FillSimulator.simulate_fills(
                    self.outstanding_orders, next_market, current_market
                )
                
                for fill in fills:
                    self.portfolio.update_fill(fill)
                    self.fills.append(fill)
            
            # Record metrics and clean up
            self._record_performance_metrics(current_market)
            self.outstanding_orders = [
                o for o in self.outstanding_orders 
                if o.status != OrderStatus.FILLED
            ]
        
        return self._generate_results()
    
    def _row_to_market_data(self, row) -> MarketData:
        """Convert DataFrame row to MarketData object"""
        return MarketData(
            timestamp=row['timestamp'],
            bids=row['bids'],
            asks=row['asks'],
            taker_buy_volume=row['taker_buy_base_volume'],
            taker_sell_volume=row['taker_sell_base_volume'],
            close_price=row['close']
        )
    
    def _refresh_orders(self, market_data: MarketData):
        """Refresh orders using the strategy"""
        # Cancel existing orders
        for order in self.outstanding_orders:
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED
        
        # Generate new orders
        bid_orders, ask_orders = self.strategy.calculate_quotes(
            market_data, self.portfolio, self.historical_data
        )
        
        # Add new orders
        self.outstanding_orders.extend(bid_orders + ask_orders)
    
    def _record_performance_metrics(self, market_data: MarketData):
        """Record performance metrics at current timestamp"""
        unrealized_pnl = self.portfolio.get_unrealized_pnl(market_data.close_price)
        total_value = self.portfolio.get_total_value(market_data.close_price)
        
        self.performance_metrics.append({
            'timestamp': market_data.timestamp,
            'base_balance': self.portfolio.base_balance,
            'quote_balance': self.portfolio.quote_balance,
            'realized_pnl': self.portfolio.realized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'total_pnl': self.portfolio.realized_pnl + unrealized_pnl,
            'total_value': total_value,
            'close_price': market_data.close_price,
            'num_outstanding_orders': len([o for o in self.outstanding_orders if o.status == OrderStatus.PENDING])
        })
    
    def _generate_results(self) -> Dict[str, Any]:
        """Generate final backtest results"""
        if not self.performance_metrics:
            return {}
        
        final_metrics = self.performance_metrics[-1]
        initial_value = self.performance_metrics[0]['total_value']
        
        # Calculate additional metrics
        total_fills = len(self.fills)
        buy_fills = len([f for f in self.fills if f.side == OrderSide.BUY])
        sell_fills = len([f for f in self.fills if f.side == OrderSide.SELL])
        
        return {
            'strategy_name': self.strategy.name,
            'total_fills': total_fills,
            'buy_fills': buy_fills,
            'sell_fills': sell_fills,
            'final_realized_pnl': final_metrics['realized_pnl'],
            'final_unrealized_pnl': final_metrics['unrealized_pnl'],
            'final_total_pnl': final_metrics['total_pnl'],
            'initial_portfolio_value': initial_value,
            'final_portfolio_value': final_metrics['total_value'],
            'total_return_pct': ((final_metrics['total_value'] - initial_value) / initial_value) * 100,
            'performance_timeseries': pd.DataFrame(self.performance_metrics),
            'fills': self.fills,
            'portfolio': self.portfolio
        }