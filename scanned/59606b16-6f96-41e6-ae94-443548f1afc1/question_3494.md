# Q3494: data and element segment initialisation cost — profile.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, maximal data and element segments with overlapping and out-of-range offsets, with function, local, table, and nesting limits all at their exact maxima, and additionally with a module one structural unit past a preparation limit, reach `get_ext_cost` in `runtime/near-vm-runner/src/profile.rs` and make instantiation cost unpriced work, or accept an out-of-range segment inconsistently, breaking the invariant that segment initialisation is validated and priced before instantiation, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/profile.rs` :: `get_ext_cost`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: maximal data and element segments with overlapping and out-of-range offsets; with function, local, table, and nesting limits all at their exact maxima; with a module one structural unit past a preparation limit
- Exploit idea: make instantiation cost unpriced work, or accept an out-of-range segment inconsistently
- Invariant to test: segment initialisation is validated and priced before instantiation
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test over overlapping and out-of-range segment offsets
