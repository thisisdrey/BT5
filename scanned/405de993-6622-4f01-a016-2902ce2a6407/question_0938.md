# Q0938: imports surface exposing a non-gated host function — mod.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, a module importing a host function gated behind a protocol feature that is not yet active, with function, local, table, and nesting limits all at their exact maxima, reach the primary handler in this file in `runtime/near-vm-runner/src/wasmtime_runner/mod.rs` and call a function that some nodes expose and others do not, breaking the invariant that the import surface is a deterministic function of the executing protocol version, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `runtime/near-vm-runner/src/wasmtime_runner/mod.rs` :: primary handler
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: a module importing a host function gated behind a protocol feature that is not yet active; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: call a function that some nodes expose and others do not
- Invariant to test: the import surface is a deterministic function of the executing protocol version
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test asserting the import list per protocol version
