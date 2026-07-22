Q16262: registration-side effect bug in anchored provider band clamp when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices while the provider uses a synthetic ratio between two oracle feeds, so that permissionless registration re-enables or binds more than the intended `(feedId, pool)` relation along `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math`, corrupting `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool? A public trader cannot change trusted roles, but can absolutely choose the swap timing and direction when the live quote is sitting on the clamp boundary. Use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices
- Exploit idea: Reach `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math` in a live public flow and show that use public registration calls to clear blacklist or activate a pool association that later price reads trust too broadly. The exact value at risk is `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Invariant to test: Permissionless registration must only authorize the exact pool-feed relation paid for by the caller. The concrete assertion should cover `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Expected Immunefi impact: High if unauthorized pools or stale blacklist states can influence production reads.
- Fast validation: Drive swaps when the anchor quote is near every band edge and assert the pool never receives a quote tighter than the intended reference envelope.
