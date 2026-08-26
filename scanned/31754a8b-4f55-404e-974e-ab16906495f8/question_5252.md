# Q5252: light-client style outcome proof for a receipt — view.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, an execution outcome proof for a receipt the attacker created with chosen ids, with a value stored directly at a full 16-way branch node, and additionally when the same value is written and deleted by many accounts in one chunk, reach `to_raw_trie_node_with_size` in `core/store/src/trie/mem/node/view.rs` and produce two receipts whose outcome ids collide so one proof attests to the other, breaking the invariant that outcome ids are collision-resistant and bind the full receipt, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/store/src/trie/mem/node/view.rs` :: `to_raw_trie_node_with_size`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: an execution outcome proof for a receipt the attacker created with chosen ids; with a value stored directly at a full 16-way branch node; when the same value is written and deleted by many accounts in one chunk
- Exploit idea: produce two receipts whose outcome ids collide so one proof attests to the other
- Invariant to test: outcome ids are collision-resistant and bind the full receipt
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on outcome-id derivation
