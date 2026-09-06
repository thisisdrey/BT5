### Title
Unauthenticated ~16MB heap-allocation amplification via sparse `BitField` decoding in `MemPoolSyncData::BloomFilter` (`/v2/mempool/query`) - (File: stackslib/src/util_lib/bloom.rs)

### Summary
`decode_bitfield`'s sparse branch validates `vec_len` only against `MAX_MESSAGE_LEN.saturating_sub(5)` and validates `num_filled` only via `should_use_sparse_encoding`, both of which are satisfiable with `num_filled = 0`, before allocating `vec![0u8; vec_len as usize]`. Because `vec_len` is a self-declared field independent of how many bytes the attacker actually has to send on the wire, a single ~51-byte unauthenticated HTTP POST to `/v2/mempool/query` can force a ~16MB heap allocation server-side. The subsequent `ret.get_mut(idx as usize).ok_or_else(...)` correctly rejects any out-of-bounds `idx`, so there is no panic/OOB-index vulnerability in the loop itself — only the allocation-amplification issue is real.

### Finding Description
In `decode_bitfield` (`stackslib/src/util_lib/bloom.rs:115-154`):
```
let vec_len: u32 = read_next(fd)?;
if vec_len > MAX_MESSAGE_LEN.saturating_sub(5) { ... }
let num_filled: u32 = read_next(fd)?;
if !should_use_sparse_encoding(num_filled as usize, vec_len as usize) { ... }
let mut ret = vec![0u8; vec_len as usize];
``` [1](#0-0) 

`should_use_sparse_encoding(0, vec_len) = 4 < vec_len`, so setting `num_filled = 0` trivially passes the gate for any `vec_len > 4`, meaning the check does not prevent an attacker from declaring `vec_len` near `MAX_MESSAGE_LEN - 5` (~16,777,211) while sending zero index/value pairs afterward. [2](#0-1) 

The loop that follows reads `num_filled` (attacker-controlled, can be 0) index/value pairs and safely bounds-checks each via `ret.get_mut(idx as usize).ok_or_else(...)`, returning `DeserializeError` rather than panicking — this part of the code is correctly guarded. [3](#0-2) 

The `BitField` and `BloomFilter<BloomNodeHasher>` types wrapping this decoder are reachable from the network via `MemPoolSyncData::BloomFilter`, which is the request body type for the mempool-sync RPC endpoint (`security: []`, i.e., unauthenticated, per the OpenAPI spec): [4](#0-3) 

Because HTTP request bodies are read up to the declared `Content-Length`, the attacker's actual wire payload size is entirely decoupled from the `vec_len` value embedded inside it. A minimal well-formed request need only contain: `MemPoolSyncDataID(1)` + hasher-type(1) + seed(32) + `num_hashes`(4) + `num_bits`(4) + sparse-encoding-marker(1) + `vec_len`(4) + `num_filled=0`(4) = 51 bytes, with `num_bits` chosen so `BITVEC_LEN!(num_bits) == vec_len` to pass the length-consistency check in `BitField::consensus_deserialize`: [5](#0-4) 

This yields a ~16MB allocation triggered by a 51-byte request — an amplification factor of roughly 330,000x, with no requirement to actually transfer 16MB of attacker data.

### Impact Explanation
Each malicious 51-byte request forces the receiving node to allocate ~16MB on the heap for a `Vec<u8>` that is immediately discarded once the length-mismatch check in `BitField::consensus_deserialize` fails (unless `num_bits` is also crafted to match, in which case the filter is accepted and retained for the duration of the mempool-sync computation). Either way, the allocation cost is paid immediately on receipt of a tiny message. Because the attack is bandwidth-decoupled, an attacker can issue many such requests in quick succession (limited only by connection/request throughput, not by the 16MB payload size), driving up server memory pressure/allocator churn far faster than the equivalent legitimate traffic volume would require. This matches "bounded per-message ~16MB allocation DoS" against an unauthenticated RPC endpoint.

### Likelihood Explanation
No privileged role, secret, or prior handshake state is required: `/v2/mempool/query` is explicitly unauthenticated per the OpenAPI spec (`security: []`). Any remote party who can open an HTTP connection to the node's RPC port can send the crafted body. The attack is trivially repeatable across many requests/connections and costs the attacker only ~51 bytes of upload per triggered 16MB allocation.

### Recommendation
In `decode_bitfield`'s sparse branch, tighten the gate so that a small `num_filled` cannot justify an arbitrarily large `vec_len`: require `vec_len` to be bounded by a value proportional to `num_filled` (e.g., enforce the `should_use_sparse_encoding` inequality strictly, and additionally cap `vec_len` to a small multiple of `num_filled * 5` or to the maximum plausible bitfield size for a bloom filter, e.g. derived from `MAX_BLOOM_COUNTER_TXS`), and/or defer the `vec![0u8; vec_len]` allocation until after validating that `vec_len` is consistent with an expected/maximum bloom-filter bit count for this protocol context (the caller already knows the max legitimate `num_bits` from `bloom_hash_count(BLOOM_COUNTER_ERROR_RATE, MAX_BLOOM_COUNTER_TXS)`); reject early if `vec_len` exceeds that bound instead of only checking against the global `MAX_MESSAGE_LEN`.

### Proof of Concept
Rust test plan (net-level) against `stacks-core--009`:
```rust
// Craft minimal bytes for MemPoolSyncData::BloomFilter with sparse BitField:
// [MemPoolSyncDataID::BloomFilter=0x01]
// [BloomHashID::BloomNodeHasher=0x01][seed: 32 bytes]
// [num_hashes: u32 = 1]
// [num_bits: u32 = N]                      // choose N = 8 * vec_len
// [BitFieldEncoding::Sparse = 0x01]
// [vec_len: u32 = MAX_MESSAGE_LEN - 5]      // ~16_777_211
// [num_filled: u32 = 0]
let bytes = build_bytes(...); // ~51 bytes total
assert_eq!(bytes.len(), 51);
let start_mem = allocator_bytes_in_use();
let _ = MemPoolSyncData::consensus_deserialize(&mut &bytes[..]);
let peak_alloc = allocator_bytes_in_use() - start_mem; // assert ~16MB spike from a 51-byte input
```
Separately, confirm no panic path exists for the `get_mut` guard:
```rust
// sparse bytes: vec_len = 4, num_filled = 1, idx = 100 (>= vec_len)
let bytes = ...; 
let result = decode_bitfield(&mut &bytes[..]);
assert!(matches!(result, Err(codec_error::DeserializeError(_)))); // no panic
```

### Citations

**File:** stackslib/src/util_lib/bloom.rs (L81-83)
```rust
fn should_use_sparse_encoding(non_zero_bytes: usize, total_bytes: usize) -> bool {
    non_zero_bytes * 5 + 4 < total_bytes
}
```

**File:** stackslib/src/util_lib/bloom.rs (L118-140)
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
            for _ in 0..num_filled {
                let idx: u32 = read_next(fd)?;
                let slot = ret.get_mut(idx as usize).ok_or_else(|| {
                    codec_error::DeserializeError(format!("Index overflow: {idx} >= {vec_len}"))
                })?;
                let value: u8 = read_next(fd)?;
                *slot = value;
            }
```

**File:** stackslib/src/util_lib/bloom.rs (L164-175)
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
```

**File:** stackslib/src/core/mempool.rs (L194-213)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<MemPoolSyncData, codec_error> {
        let data_id: u8 = read_next(fd)?;
        match MemPoolSyncDataID::from_u8(data_id).ok_or(codec_error::DeserializeError(format!(
            "Unrecognized MemPoolSyncDataID {}",
            &data_id
        )))? {
            MemPoolSyncDataID::BloomFilter => {
                let bloom_filter: BloomFilter<BloomNodeHasher> = read_next(fd)?;

                // hash parameters must be valid for the mempool
                let (_, num_hashes) =
                    bloom_hash_count(BLOOM_COUNTER_ERROR_RATE, MAX_BLOOM_COUNTER_TXS);
                if bloom_filter.num_hashes > num_hashes {
                    return Err(codec_error::DeserializeError(format!(
                        "Too many bloom hashers (max {})",
                        num_hashes
                    )));
                }
                Ok(MemPoolSyncData::BloomFilter(bloom_filter))
            }
```
