# Q5109: view-only RPC path diverging from consensus state after resharding — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, queries pinned to blocks straddling a layout change with chosen account ids, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `decode_symbol` in `core/primitives-core/src/universal_account_id.rs` and have public RPC serve state from a retired shard, misleading dependent protocols, breaking the invariant that view state resolution follows the layout of the queried block exactly, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives-core/src/universal_account_id.rs` :: `decode_symbol`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: queries pinned to blocks straddling a layout change with chosen account ids; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: have public RPC serve state from a retired shard, misleading dependent protocols
- Invariant to test: view state resolution follows the layout of the queried block exactly
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test querying straddling blocks and comparing against chunk state
