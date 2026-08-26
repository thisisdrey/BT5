# Q1904: yield/resume interacting with account deletion — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, a yield created by an account deleted before the resume arrives, when a referencing account is deleted while others still reference the code, reach `code` in `core/primitives/src/universal_state_init.rs` and resume into a nonexistent account, or strand the yielded gas forever, breaking the invariant that a yield resolves even if its creator disappears, without minting or destroying value, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/universal_state_init.rs` :: `code`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: a yield created by an account deleted before the resume arrives; when a referencing account is deleted while others still reference the code
- Exploit idea: resume into a nonexistent account, or strand the yielded gas forever
- Invariant to test: a yield resolves even if its creator disappears, without minting or destroying value
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting a yield creator before resume
