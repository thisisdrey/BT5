# Q1465: check_health_and_verify_unchanged: order close path can destroy or duplicate active exposure [an-order-followed-immediately-by] [keeper-role]

## Question
Can an unprivileged attacker route `place_order` through `check_health_and_verify_unchanged` with an order followed immediately by execution or close in the same investigation so closing or executing an order destroys, duplicates, or misattributes active exposure, violating `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: an order followed immediately by execution or close in the same investigation
- Exploit idea: Probe order bookkeeping around partial fills, close-before-settle, and multiple active order counts. Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Run partial/edge execution paths and assert order counts, balances, and liabilities remain conserved and correctly attributed. Test owner, keeper, and attacker identities across every order phase and assert only the intended role can progress each state edge.
