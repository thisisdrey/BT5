[1](#0-0) 

The premise of this question is mathematically false, not just practically mitigated.

`BitMap<BTreeMap<u32,u32>>::split_word_mask` computes `word = n >> BITS_FOR_BIT_POS` and `bit_mask = 1 << (n & bit_pos_mask())`, where `BITS_FOR_BIT_POS = (32).ilog2() = 5` for a `u32` key/value pair. [2](#0-1) [3](#0-2) 

Since the 32-bit nonce is split into exactly 27 bits (word/key) + 5 bits (bit position) with no overlap and no bits discarded (27 + 5 = 32), the mapping `n ↦ (word, bit_pos)` is a **bijection** over all `u32` values, not a hash. Every distinct `u32` nonce decomposes to a unique `(word, bit_pos)` pair, and conversely every `(word, bit_pos)` pair reconstructs to exactly one `u32` nonce (`word << 5 | bit_pos`). There is no scenario where two *different* `u32` nonce values (`N1 ≠ N2`) produce the same `(word, bit_pos)` — that would require `N1 = word<<5|bit_pos = N2`, a contradiction. The `N2 = N1 XOR (1<<31)` construction proposed in the question does not preserve `(word, bit_pos)`: XOR-ing bit 31 changes the top bits, which changes `word` (since bit 31 is part of the 27-bit word, not the 5-bit position), so `N2` lands in a completely different word/slot in the `BTreeMap`, not the same slot as any nonce derived from `N1`.

This differs fundamentally from a hash-based bitmap collision (which the question's framing implies) — here the "key" is a direct arithmetic decomposition of the input, so collisions of the type described (`different logical nonce, same physical bit`) cannot occur by construction. The `commit` function correctly checks both `self.old.get_bit(nonce)` and `self.current.set_bit(nonce)` for the exact same `nonce` value: [4](#0-3) 

Additionally, as the question itself acknowledges, the wallet `nonce` field is part of the `Request` that the victim signs — an unprivileged attacker/relayer cannot alter the victim's chosen nonce without invalidating the victim's signature, since the nonce is inside the signed payload verified before commit. The documented client-side recommendation (semi-sequential/randomized nonce generation for concurrent signers, `ConcurrentNonces`) is a storage-efficiency guideline, not a security boundary against bit collisions, because no such collision is possible in the first place. [5](#0-4) 

#No vulnerability found for this question.

### Citations

**File:** crates/bitmap/src/lib.rs (L26-27)
```rust
    #[allow(clippy::as_conversions)]
    const BITS_FOR_BIT_POS: usize = (size_of::<M::K>() * 8).ilog2() as usize;
```

**File:** crates/bitmap/src/lib.rs (L171-188)
```rust
    #[inline]
    fn get_mut_with_mask(&mut self, n: M::K) -> (impl DerefMut<Target = M::V>, M::V) {
        let (word, bit_mask) = Self::split_word_mask(n);
        (self.0.entry_or_default(word), bit_mask)
    }

    /// Returns `(word, bit_pos_mask)`
    #[inline]
    fn split_word_mask(n: M::K) -> (M::K, M::V) {
        let word = n >> Self::BITS_FOR_BIT_POS;
        let bit_mask = M::V::one() << (n & Self::bit_pos_mask());
        (word, bit_mask)
    }

    #[inline]
    fn bit_pos_mask() -> M::V {
        (M::V::one() << Self::BITS_FOR_BIT_POS) - M::V::one()
    }
```

**File:** contracts/wallet/src/nonces.rs (L128-130)
```rust
        if self.old.get_bit(nonce) || self.current.set_bit(nonce) {
            return Err(NonceError::AlreadyUsed);
        }
```

**File:** contracts/wallet/README.md (L116-129)
```markdown
* For *concurrent* **non-coordinated** signers (e.g. when a user might sign two
  requests concurrently with the same key from different devices), it's
  recommended to generate nonces *semi-sequentially*, i.e. where the nonce is randomized after each 32 sequential ones:

  ```rust,ignore
  const BIT_POS_MASK: u32 = 0b11111;

  if self.next_nonce & BIT_POS_MASK == 0 {
      self.next_nonce = random_u32() & !BIT_POS_MASK;
  }

  let nonce = self.next_nonce;
  self.next_nonce += 1;
  ```
```
