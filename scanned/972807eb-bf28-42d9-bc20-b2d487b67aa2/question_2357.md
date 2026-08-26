# Q2357: light-client style outcome proof for a receipt — resharding.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, an execution outcome proof for a receipt the attacker created with chosen ids, with keys producing maximal-length extension nodes, reach `get_interval_for_copy_to_one_child` in `core/store/src/trie/ops/resharding.rs` and produce two receipts whose outcome ids collide so one proof attests to the other, breaking the invariant that outcome ids are collision-resistant and bind the full receipt, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/store/src/trie/ops/resharding.rs` :: `get_interval_for_copy_to_one_child`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: an execution outcome proof for a receipt the attacker created with chosen ids; with keys producing maximal-length extension nodes
- Exploit idea: produce two receipts whose outcome ids collide so one proof attests to the other
- Invariant to test: outcome ids are collision-resistant and bind the full receipt
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on outcome-id derivation
