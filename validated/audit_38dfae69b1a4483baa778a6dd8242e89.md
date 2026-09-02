#No vulnerability found for this question. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

The trace confirms the binding holds. `VersionedNonce::maybe_from` returning `None` for a nonce lacking `VERSIONED_MAGIC_PREFIX` causes `verify_intent_nonce` to skip salt/deadline checks by design — this is the documented "Legacy nonce" path, explicitly still supported (`README.md` lines 18-27, `CHANGELOG.md` line 18: "Legacy nonces can't be cleared before a complete prohibition on its usage"). Skipping those checks does not remove the single-use enforcement: `commit_nonce` still routes through `MaybeLegacyNonces::commit` → `Nonces::commit` → `BitMap256::set_bit`, which returns the previous bit value and causes `Nonces::commit` to return `Err(DefuseError::NonceUsed)` on any second attempt to set an already-set bit. This is exactly the behavior proven by the existing proptest `commit_duplicate_nonce` in `contracts/defuse/src/contract/accounts/account/nonces.rs` (lines 168-179), which asserts the second `commit` of an identical nonce returns `NonceUsed`. Replaying the identical signed `MultiPayload` a second time therefore fails at `self.state.commit_nonce(signer_id, nonce)` before any funds move again, so the number of successful fund-moving executions of one signed payload remains 1, matching the claimed binding both before and after the legacy-bypass branch. There is no divergence, no double-settlement, and no unauthorized fund movement — this is intended, documented legacy-nonce behavior, not a vulnerability.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L86-111)
```rust
    fn verify_intent_nonce(&self, nonce: Nonce, intent_deadline: Timestamp) -> Result<()> {
        let Some(nonce) = VersionedNonce::maybe_from(nonce) else {
            return Ok(());
        };

        match nonce {
            VersionedNonce::V1(SaltedNonce {
                salt,
                nonce: ExpirableNonce { deadline, .. },
            }) => {
                if !self.state.is_valid_salt(salt) {
                    return Err(DefuseError::InvalidSalt);
                }

                if intent_deadline > deadline {
                    return Err(DefuseError::DeadlineGreaterThanNonce);
                }

                if deadline < Timestamp::now() {
                    return Err(DefuseError::NonceExpired);
                }
            }
        }

        Ok(())
    }
```

**File:** contracts/defuse/core/src/nonce/versioned.rs (L25-28)
```rust
    pub fn maybe_from(n: Nonce) -> Option<Self> {
        let mut versioned = n.strip_prefix(&Self::VERSIONED_MAGIC_PREFIX)?;
        Self::deserialize_reader(&mut versioned).ok()
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

**File:** contracts/defuse/README.md (L24-27)
```markdown
### Legacy nonce
A unique 256-bit randomly generated number that was initially used without additional checks.

**<span style="color: red;">But in the near future, will be prohibited and the only valid nonce schema will be a versioned nonce.</span>**
```
