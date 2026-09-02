This confirms the exact structure needed to resolve the claim.

The full 32-byte `Nonce` for `VersionedNonce::V1` is `MAGIC_PREFIX (4) || VERSION (1) || SALT (4) || DEADLINE (8) || RANDOM (15)`, and this entire 32-byte value is the exact key used by `BitMap256` (word = first 31 bytes, bit = last byte) via `Nonces::commit` -> `BitMap256::set_bit` in `crates/bitmap/src/b256.rs`. [1](#0-0) [2](#0-1) [3](#0-2) 

Since the attacker resubmits the "exact same payload," the salt, deadline, and random bytes are byte-for-byte identical, so the derived `Nonce` (the bitmap key) is identical on both submissions — the salt is embedded inside the nonce value itself, not external metadata excluded from the key. `Nonces::commit` calls `BitMap256::set_bit`, which returns the previous bit value; on the first call it returns `false` (bit set 0→1, success), and on the second call with the identical nonce it returns `true` because the bit is already 1, causing `commit` to return `DefuseError::NonceUsed` immediately. [2](#0-1) [4](#0-3) 

This happens in `Engine::execute_signed_intent` before any balance-affecting intent logic runs: `verify_intent_nonce` only checks salt validity/deadlines, and `commit_nonce` is what actually binds the nonce — and it is called unconditionally on every submission, regardless of how many valid salts currently exist in `SaltRegistry`. [5](#0-4) 

The premise that "salt-rotation history is not part of the bitmap key" is true but irrelevant to double-submission, because the bitmap key doesn't need salt-rotation history — it already contains the literal salt bytes as part of the fixed 32-byte nonce, which is reused verbatim on replay. The claimed equality ("number of times this nonce's bit is set from 0→1 == 1, independent of valid salts") actually holds correctly as an invariant, not an exploitable divergence: the second identical submission finds the bit already 1 and fails with `NonceUsed`, exactly as intended, confirmed by the existing test `test_cleanup_nonces` / nonce validity tests exercising salted nonces. [6](#0-5) 

The first half of the scenario (pre-signing with a currently-valid salt, holding it, and submitting later after the DAO naturally rotates salts) is also not a vulnerability — it's documented, intended behavior: `SaltRegistry::is_valid` returns true for both the current salt and any previous salt not explicitly invalidated, and `README.md`/`salts.rs` explicitly state old salts remain valid until explicit invalidation. [7](#0-6)  This is a designed nonce-liveness feature (letting in-flight signed intents survive routine salt rotation, similar to a "salt epoch" grace period), not a security flaw, and it does not by itself allow double-spend since nonce uniqueness is enforced independently via the bitmap.

No vulnerability found for this question.

### Citations

**File:** contracts/defuse/core/src/nonce/versioned.rs (L9-18)
```rust
/// To distinguish between legacy nonces and versioned nonces
/// we use a specific prefix individual for each version.
/// Serialized versioned nonce contains:
///     `VERSIONED_MAGIC_PREFIX (4 bytes) || VERSION (1 byte) || NONCE_BYTES (27 bytes)`
/// Currently supported versions:
///     - V1: `SALT (4 bytes) || DEADLINE (8 bytes) || NONCE (15 random bytes)`
#[derive(Clone, Debug, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub enum VersionedNonce {
    V1(SaltedNonce<ExpirableNonce<[u8; 15]>>),
}
```

**File:** contracts/defuse/core/src/nonce/mod.rs (L38-45)
```rust
    #[inline]
    pub fn commit(&mut self, n: Nonce) -> Result<()> {
        if self.0.set_bit(n) {
            return Err(DefuseError::NonceUsed);
        }

        Ok(())
    }
```

**File:** crates/bitmap/src/b256.rs (L53-60)
```rust
    /// Set the bit `n` and return old value
    #[inline]
    pub fn set_bit(&mut self, n: U256) -> bool {
        let (byte, mask) = self.get_mut_byte_with_mask(n);
        let old = *byte & mask != 0;
        *byte |= mask;
        old
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

**File:** tests/src/tests/defuse/accounts/nonces.rs (L209-232)
```rust
    // nonce can't be committed with invalidated salt
    {
        let current_salt = env.defuse.current_salt().await.unwrap();
        user.defuse_invalidate_salts(env.defuse.contract_id(), [current_salt])
            .await
            .expect("unable to invalidate salt");

        let deadline = current_timestamp + timeout_delta;
        let invalid_salt_nonce = create_random_salted_nonce(current_salt, deadline, &mut rng);

        env.defuse_simulate_and_execute_intents(
            env.defuse.contract_id(),
            [user
                .sign_defuse_message(
                    env.defuse.contract_id(),
                    invalid_salt_nonce,
                    deadline,
                    DefuseIntents { intents: [].into() },
                )
                .await],
        )
        .await
        .assert_err_contains("invalid salt");
    }
```

**File:** contracts/defuse/src/contract/state/salt_registry.rs (L79-82)
```rust
    #[inline]
    pub fn is_valid(&self, salt: Salt) -> bool {
        salt == self.current || self.previous.get(&salt).is_some_and(|v| *v)
    }
```
