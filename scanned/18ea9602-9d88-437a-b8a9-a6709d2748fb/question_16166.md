Q16166: pair-binding mismatch in anchored provider band clamp when the provider is in reference mode with no custom source

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices while the provider is in reference mode with no custom source, so that the provider and pool token pair look compatible at deployment but diverge at runtime along `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math`, corrupting `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool? A public trader cannot change trusted roles, but can absolutely choose the swap timing and direction when the live quote is sitting on the clamp boundary. Deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::getBidAndAskPrice and _computeBidAsk
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices
- Exploit idea: Reach `pool.swap -> provider.getBidAndAskPrice -> oracle.price(feedId, pool) -> band clamp -> pool swap math` in a live public flow and show that deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics. The exact value at risk is `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Invariant to test: The provider pair consumed during live swaps must be exactly the pool pair the factory accepted. The concrete assertion should cover `bid`, `ask`, anchor `mid`, spread, `minMargin`, and the final clamped quote consumed by the pool.
- Expected Immunefi impact: Critical direct loss through wrong-pair pricing.
- Fast validation: Drive swaps when the anchor quote is near every band edge and assert the pool never receives a quote tighter than the intended reference envelope.
