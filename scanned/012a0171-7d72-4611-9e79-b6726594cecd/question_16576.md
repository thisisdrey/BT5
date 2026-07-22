Q16576: pair-binding mismatch in anchored provider synthetic ratio when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with quote and lens reads taken immediately before the user executes the live swap while the provider is in source mode and the source sits near the allowed clamp edge, so that the provider and pool token pair look compatible at deployment but diverge at runtime along `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math`, corrupting base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical? Synthetic ratio mode is valid production behavior, so any direction, division, or spread-composition bug here is squarely in scope. Deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::_readLeg and ratio-mode quote path
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: quote and lens reads taken immediately before the user executes the live swap
- Exploit idea: Reach `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math` in a live public flow and show that deploy a permissionless pool whose provider passes superficial validation while the live swap path consumes the wrong pair semantics. The exact value at risk is base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Invariant to test: The provider pair consumed during live swaps must be exactly the pool pair the factory accepted. The concrete assertion should cover base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Expected Immunefi impact: Critical direct loss through wrong-pair pricing.
- Fast validation: Compare single-feed and two-feed provider variants under the same public swap assumptions and assert the ratio path preserves pair direction and bounded spread.
