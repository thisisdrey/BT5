Q16729: provider-backed view divergence in anchored provider synthetic ratio when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with public swaps that force the pool to read its price provider at a boundary quote while the provider is in source mode and the source sits near the allowed clamp edge, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math`, corrupting base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical? Synthetic ratio mode is valid production behavior, so any direction, division, or spread-composition bug here is squarely in scope. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::_readLeg and ratio-mode quote path
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: public swaps that force the pool to read its price provider at a boundary quote
- Exploit idea: Reach `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Compare single-feed and two-feed provider variants under the same public swap assumptions and assert the ratio path preserves pair direction and bounded spread.
