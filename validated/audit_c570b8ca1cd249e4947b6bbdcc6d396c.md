No vulnerability found for this question.

**Rationale (brief):** The only code path that calls `cleanup_nonce_by_prefix` is `Contract::cleanup_nonces` in `contracts/defuse/src/contract/garbage_collector.rs`, which is gated by `#[access_control_any(roles(Role::DAO, Role::GarbageCollector))]` and requires `assert_one_yocto()`. [1](#0-0) 

This means an unprivileged attacker cannot invoke nonce-word cleanup at all — it is not reachable via `execute_intents(signed: Vec<MultiPayload>)`, which only ever calls `commit_nonce`, never `cleanup_nonce_by_prefix`. [2](#0-1) 

While it is true that `BitMap256::cleanup_by_prefix` removes the entire 248-bit-prefix word in one shot rather than checking each of the 256 individual bits for expiry — so if it were reachable with an attacker-chosen `account_id`/`prefix`, a still-live bit sharing that word could be wiped — [3](#0-2) 

this operation is restricted to `Role::DAO` / `Role::GarbageCollector` holders only, per `Contract::cleanup_nonces`. Per the audit rules, "anything requiring a DAO/role holder" is explicitly out of scope, and the attacker profile in this question is defined as unprivileged with no role. Since the described exploit path requires a role the attacker does not have, and no alternate unprivileged path to `cleanup_nonce_by_prefix` exists in the reachable code, the claimed vulnerability does not hold under the stated threat model.

### Citations

**File:** contracts/defuse/src/contract/garbage_collector.rs (L9-27)
```rust
#[near]
impl GarbageCollector for Contract {
    #[access_control_any(roles(Role::DAO, Role::GarbageCollector))]
    #[payable]
    fn cleanup_nonces(&mut self, nonces: Vec<(AccountId, Vec<AsBase64<Nonce>>)>) {
        assert_one_yocto();

        for (account_id, nonces) in nonces {
            for nonce in nonces.into_iter().map(AsBase64::into_inner) {
                if !self.is_nonce_cleanable(nonce) {
                    continue;
                }

                // NOTE: all errors are omitted
                let [prefix @ .., _] = nonce;
                let _ = State::cleanup_nonce_by_prefix(self, &account_id, prefix);
            }
        }
    }
```

**File:** contracts/defuse/core/src/engine/mod.rs (L75-83)
```rust
        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
    }
```

**File:** crates/bitmap/src/b256.rs (L48-51)
```rust
    #[inline]
    pub fn cleanup_by_prefix(&mut self, prefix: U248) -> bool {
        self.0.remove(&prefix).is_some()
    }
```
