# Q1100: alt_bn128 pairing/multiexp input validation — context.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, malformed field elements, points off the curve, and element counts at the accepted maximum, with the input length at exactly the host function's accepted maximum, reach `is_view` in `runtime/near-vm-runner/src/logic/context.rs` and get a non-canonical encoding accepted so honest nodes disagree, or make cost diverge from work, breaking the invariant that curve inputs are canonical, on-curve, and priced proportionally to the work performed, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/logic/context.rs` :: `is_view`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: malformed field elements, points off the curve, and element counts at the accepted maximum; with the input length at exactly the host function's accepted maximum
- Exploit idea: get a non-canonical encoding accepted so honest nodes disagree, or make cost diverge from work
- Invariant to test: curve inputs are canonical, on-curve, and priced proportionally to the work performed
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test feeding non-canonical encodings through the host function
