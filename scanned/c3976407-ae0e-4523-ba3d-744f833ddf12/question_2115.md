# Q2115: view-only RPC path diverging from consensus state after resharding — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, queries pinned to blocks straddling a layout change with chosen account ids, when a referencing account is deleted while others still reference the code, reach the primary handler in this file in `core/primitives-core/src/universal_state_init.rs` and have public RPC serve state from a retired shard, misleading dependent protocols, breaking the invariant that view state resolution follows the layout of the queried block exactly, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives-core/src/universal_state_init.rs` :: primary handler
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: queries pinned to blocks straddling a layout change with chosen account ids; when a referencing account is deleted while others still reference the code
- Exploit idea: have public RPC serve state from a retired shard, misleading dependent protocols
- Invariant to test: view state resolution follows the layout of the queried block exactly
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test querying straddling blocks and comparing against chunk state
