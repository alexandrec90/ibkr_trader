"""Advisory order recommendations for the owner's real Questrade accounts.

**Nothing in this package transmits an order.** Questrade reserves order placement for
partner apps, and the client here is GET-only by construction: it can read accounts,
balances, positions, open orders, symbols and quotes, and nothing else. The planner turns
that state plus a strategy's forward snapshot into limit-order tickets the owner reviews
and enters by hand. It is therefore outside the IBKR paper-trading gate
(``Settings.assert_trading_allowed``), which guards transmission, not advice.
"""
