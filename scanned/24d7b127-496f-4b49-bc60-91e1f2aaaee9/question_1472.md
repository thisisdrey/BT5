# Q1472: check_health_and_verify_unchanged: order close path can destroy or duplicate active exposure [order-state-surrounding-a-recently] [fill-rounding]

## Question
Can an unprivileged attacker route `place_order` through `check_health_and_verify_unchanged` with order state surrounding a recently refreshed bank price cache so closing or executing an order destroys, duplicates, or misattributes active exposure, violating `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: order state surrounding a recently refreshed bank price cache
- Exploit idea: Probe order bookkeeping around partial fills, close-before-settle, and multiple active order counts. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Run partial/edge execution paths and assert order counts, balances, and liabilities remain conserved and correctly attributed. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
