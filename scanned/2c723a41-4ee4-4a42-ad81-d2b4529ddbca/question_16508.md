Q16508: staleness fail-open in anchored provider synthetic ratio when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with provider registration parameters passed through the public oracle registration path while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that a quote that should be stale or sequencer-invalid is still admitted to a live swap along `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math`, corrupting base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical? Synthetic ratio mode is valid production behavior, so any direction, division, or spread-composition bug here is squarely in scope. Trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::_readLeg and ratio-mode quote path
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: provider registration parameters passed through the public oracle registration path
- Exploit idea: Reach `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math` in a live public flow and show that trade exactly at the freshness boundary and see whether the provider returns a valid-looking quote instead of failing closed. The exact value at risk is base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Invariant to test: No stale, future, or sequencer-invalid quote may reach pool swap math. The concrete assertion should cover base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Expected Immunefi impact: High bad-price execution on production swaps.
- Fast validation: Compare single-feed and two-feed provider variants under the same public swap assumptions and assert the ratio path preserves pair direction and bounded spread.
