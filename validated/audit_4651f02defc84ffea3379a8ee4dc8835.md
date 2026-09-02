### No vulnerability found for this question.

Legacy (non-versioned) nonces intentionally bypass the salt/deadline mechanism by design — this is explicitly documented in the source: `VersionedNonce::maybe_from` only recognizes nonces containing the `VERSIONED_MAGIC_PREFIX` and returns `None` for anything else, at which point `verify_intent_nonce` returns `Ok(())` unconditionally [1](#0-0) . The salted/expirable nonce scheme is explicitly called out as a supplementary, versioned-only mechanism, with a code comment stating legacy nonces "can still be used at this time, but will be prohibited out in the near future" [2](#0-1) . There is no protocol invariant anywhere that "every committed nonce must be bounded by a valid salt and deadline" — that binding only applies to `VersionedNonce::V1` nonces, and legacy nonces are a known, intentionally-supported nonce format with plain replay protection via the bitmap `Nonces::commit` [3](#0-2) .

The only scenario in which this matters is when a signer's private key has already been compromised, and salt invalidation is used as an out-of-band revocation mechanism for that compromised key's pending intents — which requires "key compromise," explicitly listed as out of scope. Absent key compromise, a signer using a legacy nonce is only affecting intents they themselves signed with their own authorization, which is not an unauthorized transfer of funds and does not meet the Critical/High impact bar (no tokens move without the owner's valid signature; the owner did sign it). Also, `deadline < Timestamp::now()` is still checked independently on every intent regardless of nonce type [4](#0-3) , so an "arbitrarily far future deadline" intent is only reachable within its own explicit self-set expiry, not indefinitely outside the normal deadline mechanism the signer chose.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L63-68)
```rust
        self.inspector.on_deadline(deadline);

        // make sure message is still valid
        if deadline < Timestamp::now() {
            return Err(DefuseError::DeadlineExpired);
        }
```

**File:** contracts/defuse/core/src/engine/mod.rs (L86-89)
```rust
    fn verify_intent_nonce(&self, nonce: Nonce, intent_deadline: Timestamp) -> Result<()> {
        let Some(nonce) = VersionedNonce::maybe_from(nonce) else {
            return Ok(());
        };
```

**File:** contracts/defuse/core/src/nonce/versioned.rs (L20-20)
```rust
// NOTE: Legacy nonces can still be used at this time, but will be prohibited out in the near future.
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
