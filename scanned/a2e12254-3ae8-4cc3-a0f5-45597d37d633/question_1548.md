# Q1548: check_health_and_verify_unchanged: order fill and accounting use inconsistent amount domains [tiny-amount-orders-that-can] [fill-rounding]

## Question
Can an unprivileged attacker call `place_order` with tiny amount orders that can round to or from dust-sized balances so `check_health_and_verify_unchanged` measures one side of an order in a different amount domain than the other, breaking `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: tiny amount orders that can round to or from dust-sized balances
- Exploit idea: Stress share amounts vs token amounts, post-fee vs pre-fee amounts, and spot vs cached values in execution and settlement. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Fuzz amount domains around execution and assert filled value, received value, and liability reduction reconcile exactly. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
