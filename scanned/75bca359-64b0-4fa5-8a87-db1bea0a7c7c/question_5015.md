# Q5015: yield/resume interacting with account deletion — manager.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, a yield created by an account deleted before the resume arrives, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `process_memtrie_resharding_storage_update` in `chain/chain/src/resharding/manager.rs` and resume into a nonexistent account, or strand the yielded gas forever, breaking the invariant that a yield resolves even if its creator disappears, without minting or destroying value, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `chain/chain/src/resharding/manager.rs` :: `process_memtrie_resharding_storage_update`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: a yield created by an account deleted before the resume arrives; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: resume into a nonexistent account, or strand the yielded gas forever
- Invariant to test: a yield resolves even if its creator disappears, without minting or destroying value
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting a yield creator before resume
