# Q5715: universal state init combined with a global contract reference — manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init whose state references a global contract withdrawn in the same block, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `get_child_congestion_info` in `chain/chain/src/resharding/manager.rs` and create an account permanently pointing at code that no longer exists, breaking the invariant that an account can never be initialised against unresolvable code, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `chain/chain/src/resharding/manager.rs` :: `get_child_congestion_info`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init whose state references a global contract withdrawn in the same block; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: create an account permanently pointing at code that no longer exists
- Invariant to test: an account can never be initialised against unresolvable code
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test initialising against a code reference removed in the same chunk
