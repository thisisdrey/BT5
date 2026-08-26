# Q2090: view-only RPC path diverging from consensus state after resharding — upgrade_schedule.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, queries pinned to blocks straddling a layout change with chosen account ids, when a referencing account is deleted while others still reference the code, reach `new_from_env_or_schedule` in `core/primitives/src/upgrade_schedule.rs` and have public RPC serve state from a retired shard, misleading dependent protocols, breaking the invariant that view state resolution follows the layout of the queried block exactly, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives/src/upgrade_schedule.rs` :: `new_from_env_or_schedule`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: queries pinned to blocks straddling a layout change with chosen account ids; when a referencing account is deleted while others still reference the code
- Exploit idea: have public RPC serve state from a retired shard, misleading dependent protocols
- Invariant to test: view state resolution follows the layout of the queried block exactly
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test querying straddling blocks and comparing against chunk state
