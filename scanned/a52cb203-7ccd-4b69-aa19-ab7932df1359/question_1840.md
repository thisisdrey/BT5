# Q1840: contract code storage keyed on a truncated hash — squash.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two contracts whose code-storage keys share a prefix used for lookup, with keys producing maximal-length extension nodes, reach `extend_child` in `core/store/src/trie/ops/squash.rs` and make an account resolve to another account's code, breaking the invariant that code lookup keys commit to the full code hash, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/ops/squash.rs` :: `extend_child`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two contracts whose code-storage keys share a prefix used for lookup; with keys producing maximal-length extension nodes
- Exploit idea: make an account resolve to another account's code
- Invariant to test: code lookup keys commit to the full code hash
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test asserting full-hash keying in the code store
