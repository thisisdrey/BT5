# Q1486: check_health_and_verify_unchanged: keeper-only action can be taken by the wrong caller [an-account-where-emode-or] [fill-rounding]

## Question
Can an unprivileged attacker use `place_order` with an account where eMode or isolated-like context changes before execution so `check_health_and_verify_unchanged` lets the wrong caller perform a keeper-only or counterparty-only action, breaking `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and leading to `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: an account where eMode or isolated-like context changes before execution
- Exploit idea: Check identity binding for order closers/executors and whether it is enforced on the exact account and phase intended. Focus specifically on partial fills and tiny remainder states where order accounting can diverge from actual value movement.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Model honest owner, honest keeper, and attacker identities and assert only the intended role can take each transition. Fuzz partial fills near one-unit and one-share boundaries and assert no order path can leak value or strand live exposure.
