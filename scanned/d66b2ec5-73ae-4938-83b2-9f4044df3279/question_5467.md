# Q5467: gas counter arithmetic near the u64 ceiling — alt_bn128.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a call sequence engineered to drive burnt/used gas close to u64::MAX, with a (ptr,len) pair whose sum overflows the address space, and additionally with a zero-length access at the last valid memory page, reach `decode_u256` in `runtime/near-vm-runner/src/logic/alt_bn128.rs` and make the gas counter wrap or saturate so execution continues past the gas limit, breaking the invariant that gas accounting is checked arithmetic and always terminates execution at the limit, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/alt_bn128.rs` :: `decode_u256`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a call sequence engineered to drive burnt/used gas close to u64::MAX; with a (ptr,len) pair whose sum overflows the address space; with a zero-length access at the last valid memory page
- Exploit idea: make the gas counter wrap or saturate so execution continues past the gas limit
- Invariant to test: gas accounting is checked arithmetic and always terminates execution at the limit
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test on the gas counter with near-ceiling values
