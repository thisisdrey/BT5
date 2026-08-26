# Q5756: view serialisation diverging from execution semantics — version.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, state that serialises differently through the view types than through the execution types, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `clamp_to_supported_protocol_version` in `core/primitives-core/src/version.rs` and have public RPC report a value that contradicts committed state, breaking the invariant that view representations are lossless projections of execution state, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives-core/src/version.rs` :: `clamp_to_supported_protocol_version`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: state that serialises differently through the view types than through the execution types; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: have public RPC report a value that contradicts committed state
- Invariant to test: view representations are lossless projections of execution state
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: differential test comparing view output against raw state
