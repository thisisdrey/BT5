Found the analog. In `pallet-broker`'s bulk-coretime sale mechanism, `Pallet::<T>::purchase_core` (`substrate/frame/broker/src/utility_impls.rs`) sets `sale.sellout_price` from the price paid on the current purchase whenever `cores_sold <= ideal_cores_sold`:

```rust
// substrate/frame/broker/src/utility_impls.rs:85-90
let core = sale.first_core.saturating_add(sale.cores_sold);
sale.cores_sold.saturating_inc();
if sale.cores_sold <= sale.ideal_cores_sold || sale.sellout_price.is_none() {
    sale.sellout_price = Some(price);
}
```

This `sellout_price` is fed directly into `AdaptPrice::adapt_price` (`substrate/frame/broker/src/adapt_price.rs:119-136`, `CenterTargetPrice::adapt_price`) at `rotate_sale` (`substrate/frame/broker/src/tick_impls.rs:178`) to set the **entire next sale's** `end_price`/`target_price` — i.e. one single trade's price becomes the baseline that determines pricing for every subsequent buyer, exactly the "single manipulable trade sets a shared running metric" pattern in the H-08 report.

### Title
Coretime sale price can be gamed by a single low-value purchase at end of leadin, understating `sellout_price` and mispricing future sales - (File: `substrate/frame/broker/src/utility_impls.rs`)

### Summary
`sale_price` decays monotonically over `leadin_length` down to `end_price` (`substrate/frame/broker/src/utility_impls.rs:62-66`). The very first "ideal" purchase in a sale sets `sale.sellout_price` to whatever price was paid at that moment (`purchase_core`, lines 85-90). An unprivileged buyer can simply wait until the price has decayed to (or near) `end_price` before making the sale's first purchase, forcing `sellout_price = end_price` (the minimum possible price for that sale) regardless of true demand.

### Finding Description
`sellout_price` is meant to reflect market-clearing demand for coretime and feed `AdaptPrice` (`CenterTargetPrice::adapt_price`, `substrate/frame/broker/src/adapt_price.rs:119-136`) so that the next sale's `end_price`/`target_price` track real demand. But the value is derived from a single purchaser's chosen price, not from aggregate volume or a time-weighted average — structurally identical to the H-08 bug class where a single manipulable trade determines a shared "average" that drives payouts/pricing for everyone. Because `purchase_core` unconditionally overwrites `sellout_price` on every purchase while `cores_sold <= ideal_cores_sold` (line 87), the *last* such purchase before the ideal threshold is crossed wins — an attacker (or a colluding buyer group) can deliberately delay their purchase to the end of the leadin period (lowest price point) to record the lowest possible `sellout_price` for that sale, artificially depressing `CenterTargetPrice::adapt_price`'s output for every future sale (`end_price = sellout_price / 10`, `target_price = sellout_price`). The same account, or colluders, can then buy the bulk of cores in the following (artificially cheap) sale at a fraction of the fair price, repeating each cycle to keep ratcheting the price down (bounded only by `MinimumPrice`, which only floors `end_price`, not `target_price`/renewal pricing dynamics), extracting value that should have accrued to `OnRevenue` (block-production revenue channel, i.e. chain treasury).

### Impact Explanation
This directly degrades the priced allocation of a scarce, safety-critical resource (parachain coretime) that underpins block production for all parachains using Coretime chains (Rococo/Westend and, per `prdoc/1.13.0/pr_4521.prdoc`, "very likely" fellowship production runtimes). Persistent underpricing lets a single well-timed actor capture coretime cheaply at the expense of the chain's own revenue (`type OnRevenue: OnUnbalanced<...>`), which is a "public underpriced work that degrades block production" class impact per the gate. This is analogous to, but distinct from, the already-acknowledged `AdaptPrice`-related manipulation (fixed partially by `pr_4521`/`pr_3636`/`pr_8630`): those fixes changed *which* price feeds the adapter and added a price floor, but did not change the fact that `sellout_price` is set from a single purchase rather than aggregated market activity, so the single-trade manipulation vector against `sellout_price` itself remains.

### Likelihood Explanation
Any signed account can call the public `purchase` extrinsic (`substrate/frame/broker/src/lib.rs:719-726`) at any point in the leadin period; timing a purchase near the end of leadin to minimize `sale_price` requires no privileged access, collusion with validators, or off-chain infrastructure — only patience within a single sale window and knowledge of the public `sale_price` decay curve (`sale_price`, `utility_impls.rs:62-66`). No admin/governance action is needed to execute the strategy.

### Recommendation
Compute `sellout_price` (and the price fed to `AdaptPrice`) from volume-weighted or time-weighted price data across the *whole* sale, not from a single purchase's spot price — e.g., track a running weighted average of `(price × cores_sold)` over the sale period, analogous to the report's own recommendation to use traded volume over a timespan instead of individual trades. Alternatively, require `sellout_price` to be derived from the price of the *last* core sold in the "ideal" band by time-order and cross-checked against a minimum sample size/spread, so a single well-timed transaction cannot set the entire next sale's baseline price.

### Proof of Concept
1. A coretime sale starts with `leadin_length = L`, `end_price = P`, decaying price via `sale_price(sale, now) = leadin_factor_at(now/L) * end_price` (`utility_impls.rs:62-66`).
2. Attacker waits until `now ≈ sale_start + L` so `sale_price ≈ end_price = P` (minimum for the sale).
3. Attacker calls `purchase(origin, price_limit = P)` (`lib.rs:719-726`) as the (or one of the) first `ideal_cores_sold` purchases; `purchase_core` sets `sale.sellout_price = Some(P)` (`utility_impls.rs:87-89`).
4. At `rotate_sale`, `T::PriceAdapter::adapt_price(SalePerformance::from_sale(&old_sale))` computes new `end_price = P/10`, `target_price = P` for the *next* sale (`CenterTargetPrice::adapt_price`, `adapt_price.rs:119-136`), even though other buyers may have paid far more than `P` earlier in the sale (their payments never fed `sellout_price` after the ideal threshold, or were overwritten by later low-price purchases).
5. Attacker (or colluders) buys the bulk of cores in the next sale at the now-depressed price, repeating the cycle each rotation to keep suppressing `end_price`/`target_price`, capturing coretime far below fair market value at the chain's revenue expense.