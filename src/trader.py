# -*- coding: utf-8 -*-
"""QMT trading wrapper - buy/sell/position management."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


@dataclass
class Order:
    order_id: int = 0
    stock_code: str = ""
    order_type: int = 0  # 23=buy, 24=sell
    price: float = 0.0
    volume: int = 0
    filled_volume: int = 0
    status: int = 0
    remark: str = ""


@dataclass
class Position:
    stock_code: str = ""
    stock_name: str = ""
    volume: int = 0
    available: int = 0
    avg_price: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    profit_pct: float = 0.0


class Trader:
    """QMT trader with buy/sell/position management."""

    def __init__(self, qmt_path: str, session: int, account_id: str) -> None:
        self.qmt_path = qmt_path
        self.session = session
        self.account_id = account_id
        self._xt_trader = None
        self._connected = False
        self._orders: dict[int, Order] = {}
        self._positions: dict[str, Position] = {}
        self._callbacks: list[Any] = []

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to QMT trading service."""
        try:
            from xtquant import xttrader

            self._xt_trader = xttrader.XtQuantTrader(self.qmt_path, self.session)
            self._xt_trader.start()
            self._xt_trader.connect()
            time.sleep(1)

            # Subscribe to account
            self._xt_trader.subscribe(self.account_id)
            time.sleep(0.5)

            self._connected = True
            _log.info("QMT trader connected: account=%s", self.account_id)

            # Load initial positions
            self._refresh_positions()
            return True
        except Exception as exc:
            _log.error("QMT trader connect failed: %s", exc)
            self._connected = False
            return False

    def disconnect(self) -> None:
        if self._xt_trader:
            try:
                self._xt_trader.stop()
            except Exception:
                pass
            self._xt_trader = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._xt_trader is not None

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _refresh_positions(self) -> dict[str, Position]:
        if not self.is_connected:
            return {}
        try:
            raw = self._xt_trader.query_stock_positions(self.account_id)
            self._positions = {}
            for p in (raw or []):
                code = p.stock_code.split(".")[0] if "." in p.stock_code else p.stock_code
                self._positions[code] = Position(
                    stock_code=code,
                    stock_name=getattr(p, "stock_name", ""),
                    volume=int(getattr(p, "volume", 0)),
                    available=int(getattr(p, "can_use_volume", getattr(p, "available", 0))),
                    avg_price=float(getattr(p, "open_price", getattr(p, "avg_price", 0))),
                    current_price=float(getattr(p, "last_price", 0)),
                    market_value=float(getattr(p, "market_value", 0)),
                    profit_pct=float(getattr(p, "profit_ratio", getattr(p, "income_ratio", 0))) * 100,
                )
            return dict(self._positions)
        except Exception as exc:
            _log.warning("Position refresh failed: %s", exc)
            return dict(self._positions)

    @property
    def positions(self) -> dict[str, Position]:
        self._refresh_positions()
        return dict(self._positions)

    def position_count(self) -> int:
        return sum(1 for p in self._positions.values() if p.volume > 0)

    def has_position(self, code: str) -> bool:
        return code in self._positions and self._positions[code].volume > 0

    # ------------------------------------------------------------------
    # Asset query
    # ------------------------------------------------------------------

    def query_asset(self) -> dict | None:
        if not self.is_connected:
            return None
        try:
            raw = self._xt_trader.query_stock_asset(self.account_id)
            if raw:
                return {
                    "total": float(getattr(raw, "total_asset", 0)),
                    "available": float(getattr(raw, "enable_amount", getattr(raw, "available", 0))),
                    "market_value": float(getattr(raw, "market_value", 0)),
                }
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def buy(self, stock_code: str, price: float, volume: int, remark: str = "qmt_strategy") -> int:
        """Place a buy order. Returns order_id or 0 on failure.

        stock_code: 6-digit code without exchange suffix (e.g., "000001")
        price: 0 for market price
        volume: number of shares (100-share lots)
        """
        if not self.is_connected:
            _log.error("Trader not connected, cannot buy %s", stock_code)
            return 0

        full_code = self._full_code(stock_code)
        price_type = 5 if price <= 0 else 11  # 5=market, 11=limit
        order_type = 23  # buy

        try:
            seq = self._xt_trader.order_stock(
                self.account_id, full_code, order_type, volume,
                price_type, price, "qmt_strategy", remark,
            )
            _log.info("BUY order sent: %s %d shares @ %s (seq=%s)", stock_code, volume, price or "market", seq)
            # seq is request sequence, actual order_id comes via callback or query
            time.sleep(0.3)
            orders = self.query_orders()
            for o in orders:
                if o.stock_code == stock_code and o.order_type == 23:
                    return o.order_id
            return 0
        except Exception as exc:
            _log.error("BUY failed for %s: %s", stock_code, exc)
            return 0

    def sell(self, stock_code: str, price: float, volume: int, remark: str = "qmt_strategy") -> int:
        """Place a sell order."""
        if not self.is_connected:
            _log.error("Trader not connected, cannot sell %s", stock_code)
            return 0

        full_code = self._full_code(stock_code)
        price_type = 5 if price <= 0 else 11
        order_type = 24  # sell

        try:
            seq = self._xt_trader.order_stock(
                self.account_id, full_code, order_type, volume,
                price_type, price, "qmt_strategy", remark,
            )
            _log.info("SELL order sent: %s %d shares @ %s (seq=%s)", stock_code, volume, price or "market", seq)
            time.sleep(0.3)
            orders = self.query_orders()
            for o in orders:
                if o.stock_code == stock_code and o.order_type == 24:
                    return o.order_id
            return 0
        except Exception as exc:
            _log.error("SELL failed for %s: %s", stock_code, exc)
            return 0

    def cancel_order(self, order_id: int) -> bool:
        if not self.is_connected:
            return False
        try:
            self._xt_trader.cancel_order_stock(self.account_id, order_id)
            return True
        except Exception:
            return False

    def query_orders(self) -> list[Order]:
        if not self.is_connected:
            return []
        try:
            raw = self._xt_trader.query_stock_orders(self.account_id)
            result = []
            for o in (raw or []):
                code = o.stock_code.split(".")[0] if "." in o.stock_code else o.stock_code
                result.append(Order(
                    order_id=int(getattr(o, "order_id", 0)),
                    stock_code=code,
                    order_type=int(getattr(o, "order_type", 0)),
                    price=float(getattr(o, "price", 0)),
                    volume=int(getattr(o, "order_volume", 0)),
                    filled_volume=int(getattr(o, "trade_volume", getattr(o, "filled_volume", 0))),
                    status=int(getattr(o, "order_status", getattr(o, "status", 0))),
                    remark=str(getattr(o, "remark", "")),
                ))
            return result
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _full_code(code: str) -> str:
        if "." in code:
            return code
        return f"{code}.{'SH' if code.startswith('6') else 'SZ'}"

    @staticmethod
    def round_lot(volume: int) -> int:
        return (volume // 100) * 100

    def calc_buy_volume(self, price: float, available_cash: float, max_ratio: float = 0.2) -> int:
        """Calculate how many shares to buy based on available cash and position ratio."""
        max_amount = available_cash * max_ratio
        shares = int(max_amount / price)
        return self.round_lot(shares)
