# Q4801: data and element segment initialisation cost — runner.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, maximal data and element segments with overlapping and out-of-range offsets, with a module one structural unit past a preparation limit, and additionally with deeply nested loops and a maximal br_table target list, reach `is_available` in `runtime/near-vm-runner/src/runner.rs` and make instantiation cost unpriced work, or accept an out-of-range segment inconsistently, breaking the invariant that segment initialisation is validated and priced before instantiation, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/runner.rs` :: `is_available`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: maximal data and element segments with overlapping and out-of-range offsets; with a module one structural unit past a preparation limit; with deeply nested loops and a maximal br_table target list
- Exploit idea: make instantiation cost unpriced work, or accept an out-of-range segment inconsistently
- Invariant to test: segment initialisation is validated and priced before instantiation
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test over overlapping and out-of-range segment offsets
