### Title
Unbounded `vec_len`-driven allocation in `decode_bitfield` sparse branch enables single-message memory amplification - (File: stackslib/src/util_lib/bloom.rs)

### Summary
`decode_bitfield`'s `Sparse` branch only checks that `vec_len` is below `MAX_MESSAGE_LEN - 5` and that `should_use_sparse_encoding(num_filled, vec_len)` holds, but that latter check is satisfied precisely when `num_filled` is *small relative to* `vec_len` — the opposite of what would bound the allocation. An attacker can therefore send a small wire payload (`vec_len` near `MAX_MESSAGE_LEN`, `num_filled = 1`) that forces a `vec![0u8; vec_len]` allocation of close to the max message size.

### Finding Description
In `decode_bitfield` [1](#0-0) , the sparse-encoding path reads `vec_len` and rejects it only if `vec_len > MAX_MESSAGE_LEN.saturating_sub(5)`. It then checks `should_use_sparse_encoding(num_filled as usize, vec_len as usize)`, which is defined as `non_zero_bytes * 5 + 4 < total_bytes` [2](#0-1) . For `num_filled = 1`, this condition becomes `9 < vec_len`, which is trivially true for any large `vec_len`. Consequently, the "sparse-encoding sanity check" does not bound the allocation size at all relative to the actual payload — it only rejects the case where the data *should* have used the dense/`Full` encoding, and is exactly satisfied by highly sparse fills such as one non-zero byte in a huge array. The code then allocates `let mut ret = vec![0u8; vec_len as usize];` [3](#0-2)  before reading any `idx`/`value` pairs, meaning the large allocation happens unconditionally as long as `num_filled` is consistent (i.e., very small) with a large `vec_len`. The wire size of such a message is tiny: 1 byte (encoding tag) + 4 bytes (`vec_len`) + 4 bytes (`num_filled`) + 5 bytes per `(idx, value)` pair, so a handful of bytes forces an allocation approaching `MAX_MESSAGE_LEN`.

This `BitField`/`BloomFilter` codec is used for bloom-filter-bearing structures such as mempool sync data, decoded via `BloomFilter<BloomNodeHasher>::consensus_deserialize` → `BitField::consensus_deserialize` → `decode_bitfield` [4](#0-3) [5](#0-4) , reachable from network-facing mempool-sync handling code (referenced by `postmempoolquery.rs`).

### Impact Explanation
Each crafted message (tens of bytes on the wire) forces the node to allocate and zero a buffer up to `MAX_MESSAGE_LEN` in size before any further validation of the actual index/value pairs occurs. A single such message is transient (the allocation is short-lived, scoped to `decode_bitfield`) and unlikely to crash a node by itself, but repeated concurrent low-bandwidth messages can drive up memory/CPU (zeroing) pressure disproportionately to the bytes sent, since the attacker's cost per allocation is a handful of bytes versus a large server-side allocation each time. This is a bounded compute/memory amplification affecting whatever endpoint processes bloom-filter-bearing mempool sync payloads.

### Likelihood Explanation
No authentication or special peer state is required — this is reachable by any party able to submit a mempool-sync bloom filter payload, and the malformed structure is trivial to construct (fixed byte layout, no signatures involved at this layer). The attack is repeatable per message and cheap for the attacker (near-zero wire bytes per triggered allocation).

### Recommendation
Bound `vec_len` relative to `num_filled` in the sparse branch (e.g., require `vec_len` to be commensurate with the expected use case, or cap `vec_len` far below `MAX_MESSAGE_LEN`, or use a lazily-sized/streamed data structure instead of eagerly allocating `vec_len` bytes before validating indices). At minimum, additionally cap `vec_len` to a small multiple of `num_filled` (consistent with `should_use_sparse_encoding`'s intent) rather than only checking it's below the global message-size ceiling.

### Proof of Concept
In `stackslib/src/util_lib/bloom.rs` test module, construct bytes manually (bypassing `encode_bitfield`, which would never produce such a case in practice, to emulate attacker-controlled framing):
```rust
let mut bytes = vec![];
write_next(&mut bytes, &(BitFieldEncoding::Sparse as u8)).unwrap();
let vec_len: u32 = MAX_MESSAGE_LEN - 5; // near-maximal, passes the vec_len check
write_next(&mut bytes, &vec_len).unwrap();
let num_filled: u32 = 1; // passes should_use_sparse_encoding(1, vec_len) since 1*5+4=9 < vec_len
write_next(&mut bytes, &num_filled).unwrap();
write_next(&mut bytes, &(0u32)).unwrap(); // idx = 0
write_next(&mut bytes, &(1u8)).unwrap();  // value

// bytes.len() is ~14 bytes total
let start = std::time::Instant::now();
let ret = decode_bitfield(&mut &bytes[..]).unwrap();
assert_eq!(ret.len(), vec_len as usize); // ~16MB allocation from a 14-byte wire payload
```
This demonstrates the disproportion between wire bytes (~14) and the resulting allocation (`vec_len` ≈ `MAX_MESSAGE_LEN`), confirming the guard does not bound allocation size relative to actual content.

### Citations

**File:** stackslib/src/util_lib/bloom.rs (L81-83)
```rust
fn should_use_sparse_encoding(non_zero_bytes: usize, total_bytes: usize) -> bool {
    non_zero_bytes * 5 + 4 < total_bytes
}
```

**File:** stackslib/src/util_lib/bloom.rs (L118-132)
```rust
        x if x == BitFieldEncoding::Sparse as u8 => {
            // sparse encoding
            let vec_len: u32 = read_next(fd)?;
            if vec_len > MAX_MESSAGE_LEN.saturating_sub(5) {
                return Err(codec_error::OverflowError("vec_len is too big".into()));
            }
            let num_filled: u32 = read_next(fd)?;

            if !should_use_sparse_encoding(num_filled as usize, vec_len as usize) {
                return Err(codec_error::OverflowError(
                    "Non-sparse bitfield should not use sparse encoding.".into(),
                ));
            }

            let mut ret = vec![0u8; vec_len as usize];
```

**File:** stackslib/src/util_lib/bloom.rs (L164-176)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<BitField, codec_error> {
        let num_bits: u32 = read_next(fd)?;
        let bits: Vec<u8> = decode_bitfield(fd)?;
        let expected_length = BITVEC_LEN!(num_bits) as usize;
        let actual_length = bits.len();
        if expected_length != actual_length {
            return Err(codec_error::DeserializeError(format!(
                "Incorrect data size for bitfield of length {num_bits}, expected {expected_length} but got {actual_length}."
            )));
        }
        Ok(BitField(bits, num_bits))
    }
}
```

**File:** stackslib/src/util_lib/bloom.rs (L330-349)
```rust
    fn consensus_deserialize<R: Read>(
        fd: &mut R,
    ) -> Result<BloomFilter<BloomNodeHasher>, codec_error> {
        let hasher_type_u8: u8 = read_next(fd)?;
        match hasher_type_u8 as u8 {
            x if x == BloomHashID::BloomNodeHasher as u8 => {
                let seed: [u8; 32] = read_next(fd)?;
                let num_hashes: u32 = read_next(fd)?;
                let bits: BitField = read_next(fd)?;
                if bits.num_bits() == 0 {
                    return Err(codec_error::DeserializeError(
                        "Bloom filter must have non-zero bin count".into(),
                    ));
                }
                Ok(BloomFilter {
                    hasher: BloomNodeHasher { seed },
                    bits,
                    num_hashes,
                })
            }
```
