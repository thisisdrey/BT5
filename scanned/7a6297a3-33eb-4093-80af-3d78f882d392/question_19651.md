Q19651: synthetic ratio direction error in provider staleness and spread rejection when the provider is in source mode and the source sits near the allowed clamp edge

Question
Can an unprivileged attacker enter through `metric-core/contracts/MetricOmmPool.sol::swap` with synthetic two-feed provider configurations created through the scoped factory path while the provider is in source mode and the source sits near the allowed clamp edge, so that two-feed ratio mode uses the right feeds but the wrong direction, spread composition, or rounding convention along `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote`, corrupting reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote? A public attacker cannot corrupt the oracle, but can absolutely attempt trades when the quote is just about to become invalid. Trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation.

Target
- File/function: smart-contracts-poc/contracts/AnchoredPriceProvider.sol and smart-contracts-poc/contracts/ProtectedPriceProvider.sol rejection boundaries
- Entrypoint: metric-core/contracts/MetricOmmPool.sol::swap
- Attacker controls: synthetic two-feed provider configurations created through the scoped factory path
- Exploit idea: Reach `public swap -> provider read -> stale/spread/guard checks -> feed-stalled or live quote` in a live public flow and show that trigger a public swap through a synthetically priced pool and see whether the ratio quote moves opposite to the intended pair orientation. The exact value at risk is reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Invariant to test: Synthetic provider mode must preserve pair direction and bounded spread exactly as documented. The concrete assertion should cover reference staleness, max-spread, price-guard checks, and whether the pool gets a rejected or accepted quote.
- Expected Immunefi impact: Critical direct loss if pools trade against an inverted or materially wrong synthetic quote.
- Fast validation: Move oracle timestamps and spreads across their exact boundaries and assert every reachable quote transition is monotonic and fails closed.
