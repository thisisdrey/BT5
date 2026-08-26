# Q5526: free-gas and promise-gas interaction — alt_bn128.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a mix of free-tier host calls and gas-weighted promise attachments in one receipt, with a (ptr,len) pair whose sum overflows the address space, and additionally with a zero-length access at the last valid memory page, reach `decode_g2` in `runtime/near-vm-runner/src/logic/alt_bn128.rs` and reach a state where more gas is distributed to promises than the receipt actually prepaid, breaking the invariant that distributed plus burnt gas never exceeds prepaid gas, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/alt_bn128.rs` :: `decode_g2`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a mix of free-tier host calls and gas-weighted promise attachments in one receipt; with a (ptr,len) pair whose sum overflows the address space; with a zero-length access at the last valid memory page
- Exploit idea: reach a state where more gas is distributed to promises than the receipt actually prepaid
- Invariant to test: distributed plus burnt gas never exceeds prepaid gas
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: property test asserting the gas identity after random host-call sequences
