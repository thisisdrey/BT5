# Q5859: protocol feature gating derived from a version comparison — views.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, activity spanning the exact block where a feature's version predicate flips, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `from_account_contract` in `core/primitives/src/views.rs` and have two nodes evaluate the predicate differently for the same block, breaking the invariant that feature activation is a total, deterministic function of the block's protocol version, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/views.rs` :: `from_account_contract`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: activity spanning the exact block where a feature's version predicate flips; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: have two nodes evaluate the predicate differently for the same block
- Invariant to test: feature activation is a total, deterministic function of the block's protocol version
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test evaluating every feature predicate at the activation height
