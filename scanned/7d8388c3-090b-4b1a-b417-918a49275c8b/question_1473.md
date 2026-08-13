# Q1473: check_health_and_verify_unchanged: keeper-only action can be taken by the wrong caller [an-order-amount-placed-exactly] [keeper-role]

## Question
Can an unprivileged attacker use `place_order` with an order amount placed exactly at the health boundary so `check_health_and_verify_unchanged` lets the wrong caller perform a keeper-only or counterparty-only action, breaking `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and leading to `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: an order amount placed exactly at the health boundary
- Exploit idea: Check identity binding for order closers/executors and whether it is enforced on the exact account and phase intended. Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Model honest owner, honest keeper, and attacker identities and assert only the intended role can take each transition. Test owner, keeper, and attacker identities across every order phase and assert only the intended role can progress each state edge.
