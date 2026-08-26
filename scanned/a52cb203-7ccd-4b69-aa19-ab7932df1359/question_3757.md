# Q3757: yield/resume interacting with account deletion — split.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, a yield created by an account deleted before the resume arrives, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `nibbles_to_account_id` in `core/store/src/trie/split.rs` and resume into a nonexistent account, or strand the yielded gas forever, breaking the invariant that a yield resolves even if its creator disappears, without minting or destroying value, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/split.rs` :: `nibbles_to_account_id`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: a yield created by an account deleted before the resume arrives; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: resume into a nonexistent account, or strand the yielded gas forever
- Invariant to test: a yield resolves even if its creator disappears, without minting or destroying value
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting a yield creator before resume
