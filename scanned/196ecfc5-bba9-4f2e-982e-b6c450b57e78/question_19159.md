Q19159: provider-backed view divergence in confidence and margin shaping when the oracle mid is valid but its spread or staleness sits close to the rejection boundary

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mutable-provider pools that later rely on the same provider assumptions during live swaps while the oracle mid is valid but its spread or staleness sits close to the rejection boundary, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check`, corrupting `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable? The user only needs a public swap when the shaped quote is near zero, inverted, or right on the clamp boundary. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and ProtectedPriceProvider.sol confidence or margin-step shaping
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mutable-provider pools that later rely on the same provider assumptions during live swaps
- Exploit idea: Reach `public swap -> provider read -> confidence adjustment and margin-step transform -> final bid/ask check` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover `confidenceParam`, `marginStep`, the shaped pre-clamp quote, and the final bid/ask the pool treats as executable.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Force confidence and margin-step shaping to every edge case and assert the final bid/ask still respects the documented one-directional safety guarantee.
