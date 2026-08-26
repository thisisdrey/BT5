# Q5738: universal state init combined with a global contract reference — split.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init whose state references a global contract withdrawn in the same block, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `descend_step` in `core/store/src/trie/split.rs` and create an account permanently pointing at code that no longer exists, breaking the invariant that an account can never be initialised against unresolvable code, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/split.rs` :: `descend_step`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init whose state references a global contract withdrawn in the same block; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: create an account permanently pointing at code that no longer exists
- Invariant to test: an account can never be initialised against unresolvable code
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test initialising against a code reference removed in the same chunk
