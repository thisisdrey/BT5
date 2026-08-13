# Q1562: check_health_and_verify_unchanged: instruction exclusivity around orders is bypassable [an-order-followed-immediately-by] [fill-rounding]

## Question
Can an unprivileged attacker combine `place_order` with an order followed immediately by execution or close in the same investigation so `check_health_and_verify_unchanged` bypasses intended instruction exclusivity and reaches a forbidden mid-order state, violating `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: an order followed immediately by execution or close in the same investigation
- Exploit idea: Attack first/last/exclusive assumptions around start/end execution to slip other user actions into the critical section. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Enumerate mixed instruction orderings and assert all non-canonical order-execution bundles are rejected. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
