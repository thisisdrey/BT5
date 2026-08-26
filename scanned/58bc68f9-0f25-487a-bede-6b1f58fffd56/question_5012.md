# Q5012: contract code storage keyed on a truncated hash — merkle_proof.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two contracts whose code-storage keys share a prefix used for lookup, with a value stored directly at a full 16-way branch node, and additionally when the same value is written and deleted by many accounts in one chunk, reach `compute_past_block_proof_in_merkle_tree_of_later_block` in `core/store/src/merkle_proof.rs` and make an account resolve to another account's code, breaking the invariant that code lookup keys commit to the full code hash, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/merkle_proof.rs` :: `compute_past_block_proof_in_merkle_tree_of_later_block`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two contracts whose code-storage keys share a prefix used for lookup; with a value stored directly at a full 16-way branch node; when the same value is written and deleted by many accounts in one chunk
- Exploit idea: make an account resolve to another account's code
- Invariant to test: code lookup keys commit to the full code hash
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test asserting full-hash keying in the code store
