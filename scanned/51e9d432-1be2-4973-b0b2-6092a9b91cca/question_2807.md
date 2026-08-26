# Q2807: deterministic account init combined with an implicit account transfer — memtries.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, a DeterministicStateInit for an id that a concurrent Transfer is also creating implicitly, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `insert_root` in `core/store/src/trie/mem/memtries.rs` and have two creation paths race so one overwrites the other's state or keys, breaking the invariant that exactly one creation path can initialise a given account id, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/store/src/trie/mem/memtries.rs` :: `insert_root`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: a DeterministicStateInit for an id that a concurrent Transfer is also creating implicitly; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: have two creation paths race so one overwrites the other's state or keys
- Invariant to test: exactly one creation path can initialise a given account id
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test racing implicit creation against deterministic init
