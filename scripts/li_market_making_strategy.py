import os
import time
from decimal import Decimal
from typing import Dict, List, Optional, Set

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
from hummingbot.remote_iface.mqtt import ETopicPublisher
from hummingbot.strategy_v2.models.order_proposal import OrderProposal
from hummingbot.strategy_v2.executors.single_order_executor.data_types import SingleOrderExecutorConfig

class LiMarketMakingConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    markets: Dict[str, Set[str]] = {}
    eta: float = 0.5
    gamma: float = 0.1
    inventory_risk_aversion: float = 0.3
    reservation_price_sensitivity: float = 1.5
    max_spread: float = 0.02
    order_refresh_interval: float = 5.0
    inventory_target: float = 0.5
    order_amount: float = 0.01


class LiMarketMakingStrategy(StrategyV2Base):

    def start(self, clock: Clock, timestamp: float) -> None:
        """
        Start the strategy.
        :param clock: Clock to use.
        :param timestamp: Current time.
        """
        self._last_timestamp = timestamp
        self.apply_initial_setting()
        if self.mqtt_enabled:
            self._pub = ETopicPublisher("performance", use_bot_prefix=True)

    async def on_stop(self):
        await super().on_stop()
        if self.mqtt_enabled:
            self._pub({controller_id: {} for controller_id in self.controllers.keys()})
            self._pub = None

    def __init__(self, connectors: Dict[str, ConnectorBase], config: LiMarketMakingConfig):
        super().__init__(connectors, config)
        self.config = config
        self._last_order_refresh_timestamp = 0

    def on_tick(self):
        super().on_tick()

        # Refresh orders periodically
        if self.current_timestamp - self._last_order_refresh_timestamp >= self.config.order_refresh_interval:
            actions = self.create_actions_proposal()
            self.executor_orchestrator.execute_actions(actions)
            self._last_order_refresh_timestamp = self.current_timestamp

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        proposals = []

        for connector_name, trading_pairs in self.config.markets.items():
            connector = self.connectors[connector_name]
            for trading_pair in trading_pairs:
                mid_price = connector.get_mid_price(trading_pair)
                inventory_ratio = self.compute_inventory_ratio(connector, trading_pair)
                reservation_price = self.compute_reservation_price(mid_price, inventory_ratio)
                spread = self.compute_optimal_spread()

                bid_price = reservation_price * (1 - spread / 2)
                ask_price = reservation_price * (1 + spread / 2)

                amount = self.config.order_amount

                bid_order = OrderProposal(
                    side=TradeType.BUY,
                    amount=amount,
                    order_type=OrderType.LIMIT,
                    price=bid_price,
                    client_order_id=None
                )
                ask_order = OrderProposal(
                    side=TradeType.SELL,
                    amount=amount,
                    order_type=OrderType.LIMIT,
                    price=ask_price,
                    client_order_id=None
                )

                for proposal in [bid_order, ask_order]:
                    executor_config = SingleOrderExecutorConfig(
                        timestamp=self.current_timestamp,
                        connector_name=connector_name,
                        trading_pair=trading_pair,
                        order=proposal,
                    )
                    proposals.append(CreateExecutorAction(config=executor_config))

        return proposals

    def compute_inventory_ratio(self, connector: ConnectorBase, trading_pair: str) -> float:
        base, quote = trading_pair.split("-")
        base_balance = connector.get_balance(base)
        quote_balance = connector.get_balance(quote)
        mid_price = connector.get_mid_price(trading_pair)
        total_value = base_balance * mid_price + quote_balance
        inventory_ratio = (base_balance * mid_price) / total_value if total_value > 0 else 0.5
        return inventory_ratio

    def compute_reservation_price(self, mid_price: float, inventory_ratio: float) -> float:
        gamma = self.config.gamma
        q = inventory_ratio - self.config.inventory_target
        return mid_price - gamma * q * mid_price

    def compute_optimal_spread(self) -> float:
        return min(self.config.max_spread, 2 * self.config.eta)

