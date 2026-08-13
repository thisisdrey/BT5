# Q1506: check_health_and_verify_unchanged: multi-phase order execution leaks a one-time fee or premium [an-order-amount-placed-exactly] [fill-rounding]

## Question
Can an unprivileged attacker make `place_order` reach `check_health_and_verify_unchanged` with an order amount placed exactly at the health boundary so an execution fee, flat fee, or premium is charged or credited inconsistently across phases, violating `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: an order amount placed exactly at the health boundary
- Exploit idea: Inspect order start/end and close paths for duplicate or skipped fee transitions under replay, partial failure, or edge amounts. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Replay or perturb the relevant phase and assert fees can neither be skipped nor charged twice. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
