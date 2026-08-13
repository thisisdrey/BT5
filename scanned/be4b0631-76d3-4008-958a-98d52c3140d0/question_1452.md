# Q1452: check_health_and_verify_unchanged: keeper or taker can settle against stale order health [tiny-amount-orders-that-can] [fill-rounding]

## Question
Can an unprivileged attacker invoke `place_order` with tiny amount orders that can round to or from dust-sized balances so `check_health_and_verify_unchanged` settles an order against stale or mismatched health data, violating `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: tiny amount orders that can round to or from dust-sized balances
- Exploit idea: Audit whether order placement/execution validates the same risk view that is later used when assets actually move. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Place the order at a threshold state, perturb the controlled context, and assert execution only succeeds when a full fresh health check still passes. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
