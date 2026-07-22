Q16124: spread sentinel misuse in anchored provider band clamp when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with provider registration parameters passed through the public oracle registration path while the provider is in reference mode with no custom source, so that a sentinel, zero, or max-spread value is decoded as a usable quote instead of a halt condition along `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math`, corrupting `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool? A public trader cannot change trusted roles, but can absolutely choose the swap timing and direction when the live quote is sitting on the clamp boundary. Wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: provider registration parameters passed through the public oracle registration path
- Exploit idea: Reach `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math` in a live public flow and show that wait for a reachable spread boundary and see whether the provider produces bid/ask values that should have been rejected. The exact value at risk is `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Invariant to test: Sentinel or excessive spread states must halt quoting before the pool receives an executable bid/ask pair. The concrete assertion should cover `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Expected Immunefi impact: High if a broken oracle state still drives real swaps.
- Fast validation: Drive swaps when the anchor quote is near every band edge and assert the pool never receives a quote tighter than the intended reference envelope.
