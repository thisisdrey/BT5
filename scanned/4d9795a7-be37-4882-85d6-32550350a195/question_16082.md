Q16082: staleness fail-open in anchored provider band clamp when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with swap direction and `priceLimitX64` choices around the current oracle mid while the provider is in reference mode with no custom source, so that a quote that should be stale or sequencer-invalid is still admitted to a live swap along `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math`, corrupting `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool? A public trader cannot change trusted roles, but can absolutely choose the swap timing and direction when the live quote is sitting on the clamp boundary. Trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: swap direction and `priceLimitX64` choices around the current oracle mid
- Exploit idea: Reach `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math` in a live public flow and show that trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed. The exact value at risk is `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Invariant to test: No stale, future, or sequencer-invalid quote may reach pool swap math. The concrete assertion should cover `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Expected Immunefi impact: High bad-price execution on production swaps.
- Fast validation: Drive swaps when the anchor quote is near every band edge and assert the pool never receives a quote tighter than the intended reference envelope.
