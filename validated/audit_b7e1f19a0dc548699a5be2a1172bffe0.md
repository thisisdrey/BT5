This confirms the vulnerability. `cleanup_by_prefix` (`BitMap256::cleanup_by_prefix` at `crates/bitmap/src/b256.rs:49-51`) removes the entire 256-bit word keyed by `word_pos` (the 31-byte prefix), which holds up to 256 individual nonce bits corresponding to different `bit_pos` values sharing the same prefix. `is_nonce_cleanable` (`contracts/defuse/src/contract/garbage_collector.rs:32-43`) only checks the attacker-controlled nonce X's own salt/deadline, but `cleanup_nonces` then calls `State::cleanup_nonce_by_prefix` on the shared prefix, wiping out the whole word including any sibling nonce Y with a different `bit_pos` but the same `word_pos` — regardless of Y's own salt validity or usage status.

### Title
Garbage collector nonce cleanup allows replay of already-committed sibling nonces sharing the same word prefix - (File: contracts/defuse/src/contract/garbage_collector.rs)

### Summary
`is_nonce_cleanable` decides cleanability per full 32-byte `Nonce`, but the actual clear operation (`cleanup_nonce_by_prefix` → `BitMap256::cleanup_by_prefix`) operates on the 31-byte prefix (`word_pos`), erasing the entire 256-bit word. Any nonce Y sharing `word_pos` with a cleanable nonce X gets its `is_used` bit reset to `false` even if Y is still valid and already used, enabling replay of Y's signed payload.

### Finding Description
The binding that must hold is: for any nonce `Y != X` sharing the same `word_pos` as `X`, `is_nonce_used(Y)_before_cleanup == is_nonce_used(Y)_after_cleanup(X)` whenever Y's own salt is still valid and Y has not expired. This binding is broken.

Code path:
1. `Contract::is_nonce_cleanable` (`contracts/defuse/src/contract/garbage_collector.rs:31-43`) evaluates `deadline < Timestamp::now() || !self.is_valid_salt(salt)` using only nonce X's own `SaltedNonce`. This returns `true` for X whose salt was rotated out, independent of any other nonce.
2. In `cleanup_nonces` (`contracts/defuse/src/contract/garbage_collector.rs:13-27`), once `is_nonce_cleanable(nonce)` is `true` for X, the code destructures `let [prefix @ .., _] = nonce;` and calls `State::cleanup_nonce_by_prefix(self, &account_id, prefix)` — i.e., it drops only the last byte (`bit_pos`) and keeps the remaining 31 bytes (`word_pos`) as the cleanup key.
3. This flows to `MaybeLegacyNonces::cleanup_by_prefix` (`contracts/defuse/src/contract/accounts/account/nonces.rs:70-72`) → `Nonces::cleanup_by_prefix` (`contracts/defuse/core/src/nonce/mod.rs:48-50`) → `BitMap256::cleanup_by_prefix` (`crates/bitmap/src/b256.rs:49-51`), which does `self.0.remove(&prefix)`, i.e. it **removes the whole 256-bit word** stored under that `word_pos` key, not a single bit.
4. Any nonce Y = `word_pos || bit_pos'` for any `bit_pos' != bit_pos_of_X` sharing the same `word_pos` will have its bit unconditionally cleared, regardless of whether Y's own salt is still valid, Y hasn't expired, and Y was already committed (`is_used(Y) == true` before cleanup).
5. Nothing in the call chain checks Y's validity before removing the shared word — the per-nonce validity check in step 1 is decoupled from the per-word granularity of the actual clear operation in step 3.

Attacker exploit: the attacker crafts nonce X = `SaltedNonce{ salt: current_valid_salt, nonce: ExpirableNonce{ deadline: far_future, nonce: bit_pos_X } }` such that `word_pos(X) == word_pos(Y)` for a target victim/self nonce Y that has already been committed via a real fund-moving `MultiPayload` (e.g. a token transfer/withdraw intent). The attacker commits X (executing some innocuous real transfer to make it a legitimate committed nonce, or even leaves X uncommitted — cleanability only depends on salt/deadline of the raw nonce value, not on whether it was ever committed, per `is_nonce_cleanable`'s logic which doesn't check `is_used` at all). After the SaltManager naturally rotates the salt (invalidating `salt` for X), the GC role holder runs routine `cleanup_nonces` including X. `is_nonce_cleanable(X)` returns `true` via `!self.is_valid_salt(salt)`. `cleanup_nonce_by_prefix` then clears the entire word, resetting `is_used(Y)` to `false` even though Y's own salt is still valid and unexpired and Y was already executed. The attacker then resubmits Y's original signed `MultiPayload` to `execute_intents`; `commit_nonce(Y)` succeeds again (since the bit was cleared), and the transfer executes a second time — a value-moving intent settles more than once without new authorization, i.e., value leaves the Verifier that the signer authorized only once.

No existing guard prevents this: `MaybeLegacyNonces::commit` (`nonces.rs:46-58`) only checks legacy maps and the current bitmap bit, both of which are now false for Y; `is_nonce_cleanable` never inspects sibling nonces sharing the same word; the GC role's `#[access_control_any(roles(Role::DAO, Role::GarbageCollector))]` and `assert_one_yocto` only gate who can invoke cleanup — they don't fix the granularity mismatch, and the GC role holder is described as running "routine" cleanup without malicious intent, so this is exploitable purely by an attacker choosing a colliding nonce value, without collusion.

### Impact Explanation
An already-settled, signed `MultiPayload` (e.g., an FT/NFT/MT withdrawal or a token-diff transfer) can be replayed and re-executed after its nonce bit is spuriously cleared as a side effect of an unrelated sibling nonce's cleanup. This causes tokens to move, be credited, or withdrawn a second time without a new valid signature/authorization — a signed payload settling more than once. This matches the Critical impact category exactly ("one signed payload settling more than once"). The blast radius covers any account whose committed nonce happens to share a 31-byte `word_pos` prefix with an attacker-craftable, now-salt-invalid nonce, including the attacker's own previously-executed transfers (self-replay) or, in principle, any other account's nonce in the same word if the attacker can predict/collide the prefix (feasible since nonces are often chosen by the caller/wallet rather than randomly enforced on-chain, and word_pos collisions across 31 bytes require targeted crafting but are entirely attacker-controlled for the attacker's own nonces).

### Likelihood Explanation
Preconditions: (1) attacker commits/knows a nonce X with a currently valid salt and a colliding `word_pos` with a target nonce Y they want to replay (trivial for the attacker's own account since they choose their own nonces); (2) a natural salt rotation occurs via `SaltManager` invalidating X's salt while Y's salt (possibly the newer one, or any still-valid one) remains valid; (3) the GC role holder performs routine `cleanup_nonces` including X. All of these are normal operational events requiring no privilege from the attacker beyond crafting their own nonce/payload — the SaltManager and GC role holder act per their normal, non-colluding duties. The attacker fully controls the choice of `bit_pos` within their own account's nonce space, making the self-replay case (clearing their own already-executed nonce Y) straightforward and repeatable across salts and accounts.

### Recommendation
Change the cleanup granularity to match the validity check: either (a) clear only the specific bit for X rather than the whole word (requires reading the word, clearing one bit, writing back, rather than `remove`), or (b) before removing a word, verify that no other set bit in that word corresponds to a nonce with a still-valid salt/unexpired deadline (expensive, effectively defeats the purpose of prefix-based cleanup). The most direct fix is to only ever clear per-bit, and use a word-level cleanup optimization only when the entire word's contents can be proven cleanable (e.g. by encoding salt/deadline info at the word granularity, or making the salt scope wide enough that the whole word always shares one salt/deadline).

### Proof of Concept
```rust
// Rust cargo test in contracts/defuse (unit test using engine::state and MaybeLegacyAccountNonces / Contract test harness)

#[test]
fn gc_word_cleanup_clears_sibling_valid_nonce() {
    // Setup: two nonces sharing the same word_pos (first 31 bytes), differing only in bit_pos (last byte)
    let word_pos: [u8; 31] = [0x11; 31];
    let x_bit_pos: u8 = 0;
    let y_bit_pos: u8 = 1;

    let mut x_nonce: Nonce = [0u8; 32];
    x_nonce[..31].copy_from_slice(&word_pos);
    x_nonce[31] = x_bit_pos;

    let mut y_nonce: Nonce = [0u8; 32];
    y_nonce[..31].copy_from_slice(&word_pos);
    y_nonce[31] = y_bit_pos;

    // X: encode as VersionedNonce::V1 with a salt that is CURRENTLY valid, long deadline.
    // Y: encode as VersionedNonce::V1 with its own (still valid) salt, long deadline.
    // (Use SaltedNonce/ExpirableNonce encoding helpers from defuse_core::nonce.)

    // Commit both nonces via State::commit_nonce (simulating real execute_intents for X, and a real
    // fund-moving intent for Y).
    state.commit_nonce(account_id.clone(), x_nonce).unwrap();
    state.commit_nonce(account_id.clone(), y_nonce).unwrap();
    assert!(state.is_nonce_used(&account_id, y_nonce)); // Y committed, binding LHS = true

    // Simulate salt rotation invalidating X's salt but not Y's (e.g. rotate salt registry so
    // X's salt is now old/invalid while Y's salt is still the active one).
    rotate_salt_invalidating(x_nonce_salt);

    // GC role holder runs routine cleanup including only X (unaware of the collision).
    contract.cleanup_nonces(vec![(account_id.clone(), vec![AsBase64(x_nonce)])]);

    // BROKEN BINDING: is_used(Y)_before == is_used(Y)_after must hold, but it does not.
    assert!(
        !state.is_nonce_used(&account_id, y_nonce),
        "sibling nonce Y was unexpectedly cleared by cleanup of unrelated nonce X"
    );

    // Exploit: resubmit Y's original signed MultiPayload; commit_nonce(Y) now succeeds again.
    let result = state.commit_nonce(account_id.clone(), y_nonce);
    assert!(result.is_ok(), "Y's nonce was replayable after GC cleanup of sibling X");
    // In a full execute_intents test, assert the associated transfer/withdraw applies a second time,
    // i.e. balances change twice for the same signed payload.
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** contracts/defuse/src/contract/garbage_collector.rs (L13-27)
```rust
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

**File:** contracts/defuse/src/contract/garbage_collector.rs (L31-43)
```rust
    #[inline]
    fn is_nonce_cleanable(&self, nonce: Nonce) -> bool {
        let Some(versioned_nonce) = VersionedNonce::maybe_from(nonce) else {
            return false;
        };

        match versioned_nonce {
            VersionedNonce::V1(SaltedNonce {
                salt,
                nonce: ExpirableNonce { deadline, .. },
            }) => deadline < Timestamp::now() || !self.is_valid_salt(salt),
        }
    }
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L46-58)
```rust
    pub fn commit(&mut self, nonce: Nonce) -> Result<()> {
        // Check legacy maps for used nonce
        if self
            .legacy
            .as_ref()
            .is_some_and(|legacy| legacy.is_used(nonce))
        {
            return Err(DefuseError::NonceUsed);
        }

        // New nonces can be committed only to the new map
        self.nonces.commit(nonce)
    }
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L70-72)
```rust
    pub fn cleanup_by_prefix(&mut self, prefix: NoncePrefix) -> bool {
        self.nonces.cleanup_by_prefix(prefix)
    }
```

**File:** contracts/defuse/core/src/nonce/mod.rs (L48-50)
```rust
    pub fn cleanup_by_prefix(&mut self, prefix: NoncePrefix) -> bool {
        self.0.cleanup_by_prefix(prefix)
    }
```

**File:** crates/bitmap/src/b256.rs (L49-51)
```rust
    pub fn cleanup_by_prefix(&mut self, prefix: U248) -> bool {
        self.0.remove(&prefix).is_some()
    }
```
