# Q5416: gas charged after the work it is meant to pay for — alt_bn128.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, input sizes at the largest value the host function accepts, with a (ptr,len) pair whose sum overflows the address space, and additionally with a zero-length access at the last valid memory page, reach `decode_fr` in `runtime/near-vm-runner/src/logic/alt_bn128.rs` and make the host function perform the expensive work before the gas charge, so an out-of-gas receipt still costs the node the work, breaking the invariant that every input-proportional operation charges gas before performing the work, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/alt_bn128.rs` :: `decode_fr`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: input sizes at the largest value the host function accepts; with a (ptr,len) pair whose sum overflows the address space; with a zero-length access at the last valid memory page
- Exploit idea: make the host function perform the expensive work before the gas charge, so an out-of-gas receipt still costs the node the work
- Invariant to test: every input-proportional operation charges gas before performing the work
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: estimator/unit test measuring wall time for a receipt that runs out of gas mid-call
