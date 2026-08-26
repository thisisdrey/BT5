# Q0814: imports surface exposing a non-gated host function — instrument_v3.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, a module importing a host function gated behind a protocol feature that is not yet active, with function, local, table, and nesting limits all at their exact maxima, reach `maybe_add_imports` in `runtime/near-vm-runner/src/prepare/instrument_v3.rs` and call a function that some nodes expose and others do not, breaking the invariant that the import surface is a deterministic function of the executing protocol version, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `runtime/near-vm-runner/src/prepare/instrument_v3.rs` :: `maybe_add_imports`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: a module importing a host function gated behind a protocol feature that is not yet active; with function, local, table, and nesting limits all at their exact maxima
- Exploit idea: call a function that some nodes expose and others do not
- Invariant to test: the import surface is a deterministic function of the executing protocol version
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test asserting the import list per protocol version
