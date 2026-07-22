Q16468: synthetic ratio direction error in anchored provider synthetic ratio when the pool uses a 6/18 token pair and non-zero min-margin or confidence settings

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with provider registration parameters passed through the public oracle registration path while the pool uses a 6/18 token pair and non-zero min-margin or confidence settings, so that two-feed ratio mode uses the right feeds but the wrong direction, spread composition, or rounding convention along `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math`, corrupting base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical? Synthetic ratio mode is valid production behavior, so any direction, division, or spread-composition bug here is squarely in scope. Trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol::_readLeg and ratio-mode quote path
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: provider registration parameters passed through the public oracle registration path
- Exploit idea: Reach `pool.swap -> AnchoredPriceProvider two-feed read -> ratio computation -> band clamp -> swap math` in a live public flow and show that trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation. The exact value at risk is base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Invariant to test: Synthetic provider mode must preserve pair direction and bounded spread exactly as documented. The concrete assertion should cover base-feed mid, quote-feed mid, combined spread, and the final ratio quote the pool treats as canonical.
- Expected Immunefi impact: Critical direct loss if pools trade against an inverted or materially wrong synthetic quote.
- Fast validation: Compare single-feed and two-feed provider variants under the same public swap assumptions and assert the ratio path preserves pair direction and bounded spread.
