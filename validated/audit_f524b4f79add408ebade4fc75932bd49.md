### No vulnerability found for this question.

The premise conflates two different things: `Nonce` is `type Nonce = U256 = [u8; 32]` [1](#0-0) , i.e. a raw fixed-size byte array that is part of the signed `DefusePayload` directly, not a value derived by re-serializing something through different encoding paths. If two `MultiPayload`s "produce" bitwise identical `Nonce` values, that is by definition the exact same 32 bytes.

`BitMap256::set_bit` computes the target word/bit purely by destructuring these raw bytes — `let [word_pos @ .., bit_pos] = n;` — with no serialization step in between: [2](#0-1) . Because the word/bit position is derived deterministically and directly from the byte array itself (not from any borsh-encoded representation that could vary), two bitwise-identical `Nonce` values always land on the exact same bit, with no possibility of "landing on a different BitMap256 word/bit" as the question hypothesizes.

`MaybeLegacyNonces::commit` correctly checks the legacy map first, then commits into the bitmap, returning `DefuseError::NonceUsed` on the second identical attempt [3](#0-2) , and `Nonces::commit` relies on `BitMap256::set_bit`'s return value to detect the already-set bit [4](#0-3) . This is exactly the intended replay-protection behavior (confirmed by the existing `commit_duplicate_nonce` test) [5](#0-4) , not a bug. There is no "encoding divergence" pathway because the nonce is never re-serialized between the signature-verification step and the bitmap commit — the same 32 raw bytes are used throughout.

### Citations

**File:** contracts/defuse/core/src/nonce/mod.rs (L15-16)
```rust
pub type Nonce = U256;
pub type NoncePrefix = U248;
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

**File:** crates/bitmap/src/b256.rs (L40-46)
```rust
    fn get_mut_byte_with_mask(&mut self, n: U256) -> (&mut u8, u8) {
        let [word_pos @ .., bit_pos] = n;
        let bitmap = self.0.entry(word_pos).or_default();
        let byte = &mut bitmap[usize::from(bit_pos / 8)];
        let byte_mask = 1 << (bit_pos % 8);
        (byte, byte_mask)
    }
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L45-58)
```rust
    #[inline]
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

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L168-179)
```rust
    proptest! {
        #[test]
        fn commit_duplicate_nonce(nonce: U256, storage_prefix in storage_prefixes()) {
            let mut new = MaybeLegacyAccountNonces::new(LookupMap::with_hasher(storage_prefix));
            new.commit(nonce).expect("First commit should succeed");

            assert!(matches!(
                new.commit(nonce),
                Err(DefuseError::NonceUsed)
            ));
        }
    }
```
