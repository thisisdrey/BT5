Q19942: provider-backed view divergence in provider staleness and spread rejection when the provider uses a synthetic ratio between two oracle feeds

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices while the provider uses a synthetic ratio between two oracle feeds, so that provider-backed lens or quoter output diverges deterministically from the live provider path used by the swap along `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote`, corrupting reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote? A public attacker cannot corrupt the oracle, but can absolutely attempt trades when the quote is just about to become invalid. Consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and smart-contracts-poc/contracts/ProtectedPriceProvider.sol rejection boundaries
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: mixed-decimal token pairs whose provider converts between 8-decimal oracle values and Q64.64 prices
- Exploit idea: Reach `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote` in a live public flow and show that consume a public quote or distance metric and execute before the state materially changes, looking for a repeatable mismatch. The exact value at risk is reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Invariant to test: Provider-backed user or integrator views must not diverge from the live swap semantics enough to cause deterministic losses. The concrete assertion should cover reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Expected Immunefi impact: Medium predictable loss-making execution from trusted views or quotes.
- Fast validation: Move oracle timestamps and spreads across their exact boundaries and assert every reachable quote transition is monotonic and fails closed.
