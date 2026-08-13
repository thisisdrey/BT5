# Q1541: check_health_and_verify_unchanged: order fill and accounting use inconsistent amount domains [remaining-accounts-that-introduce-multiple] [keeper-role]

## Question
Can an unprivileged attacker call `place_order` with remaining accounts that introduce multiple plausible priced assets so `check_health_and_verify_unchanged` measures one side of an order in a different amount domain than the other, breaking `order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value` and causing `High: protocol insolvency or unauthorized extraction through order execution`? Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.

## Target
- File/function: `programs/marginfi/src/state/order.rs` / `check_health_and_verify_unchanged`
- Entrypoint: `place_order`
- Attacker controls: remaining accounts that introduce multiple plausible priced assets
- Exploit idea: Stress share amounts vs token amounts, post-fee vs pre-fee amounts, and spot vs cached values in execution and settlement. Focus specifically on keeper/taker/owner role confusion across partial fill, close, and re-execute paths.
- Invariant to test: order placement and execution must preserve a single coherent health view and never let orders externalize unbacked value
- Expected Immunefi impact: High: protocol insolvency or unauthorized extraction through order execution
- Fast validation: Fuzz amount domains around execution and assert filled value, received value, and liability reduction reconcile exactly. Test owner, keeper, and attacker identities across every order phase and assert only the intended role can progress each state edge.
