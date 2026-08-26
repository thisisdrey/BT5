# Q2867: free-gas and promise-gas interaction — utils.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a mix of free-tier host calls and gas-weighted promise attachments in one receipt, with the input length at exactly the host function's accepted maximum, and additionally with the input length one byte past the accepted maximum, reach `split_method_names` in `runtime/near-vm-runner/src/logic/utils.rs` and reach a state where more gas is distributed to promises than the receipt actually prepaid, breaking the invariant that distributed plus burnt gas never exceeds prepaid gas, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/utils.rs` :: `split_method_names`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a mix of free-tier host calls and gas-weighted promise attachments in one receipt; with the input length at exactly the host function's accepted maximum; with the input length one byte past the accepted maximum
- Exploit idea: reach a state where more gas is distributed to promises than the receipt actually prepaid
- Invariant to test: distributed plus burnt gas never exceeds prepaid gas
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: property test asserting the gas identity after random host-call sequences
