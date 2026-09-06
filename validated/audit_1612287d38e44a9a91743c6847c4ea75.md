### Title
Sparse bloom bitfield decode allows ~16MB allocation per tiny wire message via `decode_bitfield` - (File: stackslib/src/util_lib/bloom.rs)

### Summary
`decode_bitfield` (stackslib/src/util_lib/bloom.rs:114-153) allocates `vec![0u8; vec_len as usize]` where `vec_len` is an attacker-controlled `u32` bounded only by `MAX_MESSAGE_LEN.saturating_sub(5)` (~16MB), before validating that the actual number of entries (`num_filled`) or bytes present on the wire justify that size. Because the sparse encoding only requires `num_filled` small `(index, value)` pairs to follow, an attacker can send a tiny payload (encoding byte + 4-byte `vec_len` + 4-byte `num_filled` + a handful of entries) that forces the server to allocate up to ~16MB per message.

### Finding Description
In `decode_bitfield`, the sparse branch reads `vec_len` and checks only that it does not exceed `MAX_MESSAGE_LEN - 5` [1](#0-0) . It then reads `num_filled` and validates `should_use_sparse_encoding(num_filled, vec_len)`, which only checks that `num_filled*5+4 < vec_len` — i.e., it enforces that sparse encoding is more space-efficient than dense encoding for the *claimed* `vec_len`, but does not bound `vec_len` relative to `num_filled` from below (a `num_filled` of 1 or 2 easily satisfies this for a 16MB `vec_len`) [2](#0-1) . The code then immediately allocates `vec![0u8; vec_len as usize]` [3](#0-2)  before reading any of the `num_filled` entries, i.e., before any correlation with the actual bytes present in the message.

The only guard that later rejects mismatched sizes is in `BitField::consensus_deserialize`, which compares `BITVEC_LEN!(num_bits)` (an independently-supplied `num_bits` field) against `bits.len()` (`vec_len`) — but this check happens *after* the allocation has already occurred [4](#0-3) . Thus the 16MB allocation is unconditionally performed for every crafted message regardless of whether the subsequent consistency check later fails and the value is discarded.

This code path is reachable unauthenticated through the RPC endpoint `POST /v2/mempool/query`, whose handler deserializes attacker-supplied HTTP body bytes directly into `MemPoolSyncData` (which embeds a `BloomFilter`/`BitField`) with no additional bound beyond the generic HTTP body size limit: `let mempool_body = MemPoolSyncData::consensus_deserialize(&mut body_ptr)?;` [5](#0-4) . Because the HTTP layer only reads as many bytes as `Content-Length` declares, the attacker can send a tiny body (encoding byte + `vec_len`=~16MB + `num_filled`=1 + one 5-byte entry, i.e., ~14 bytes) that still causes a full ~16MB heap allocation server-side, breaking the intended equality "bytes allocated == bytes needed for `num_filled` entries."

### Impact Explanation
Each malicious request costs the attacker on the order of tens of bytes but forces the node to allocate up to ~16MB of heap memory in `decode_bitfield`. Because this is triggered on an unauthenticated read RPC endpoint (`/v2/mempool/query`) reachable by any remote peer able to connect to the node's RPC port, and the request can be repeated rapidly and/or across many concurrent connections, an attacker can drive the node's memory usage up disproportionately to the bandwidth/resources they expend, causing memory-pressure-induced service degradation or crash (OOM). This matches the Critical category of "remote crash/unauthenticated DoS from few messages," since a single ~14-byte message triggers a large allocation and the technique amplifies with concurrency.

### Likelihood Explanation
No authentication, secret, peer handshake, or special state is required — the endpoint is a standard public RPC route. The attacker only needs the ability to open TCP connections to the node's RPC port and send a small, well-formed HTTP POST with a crafted 14-byte-ish sparse-encoded bitfield body. The attack is trivially repeatable and parallelizable across many connections, and the per-message cost to the attacker is minimal compared to the per-message cost to the victim (asymmetric amplification), making this a low-cost/high-impact vector.

### Recommendation
In `decode_bitfield`, tighten the sparse-encoding validation to bound `vec_len` relative to `num_filled` from both directions (not just the space-efficiency inequality), e.g., require `vec_len` to also be consistent with an expected/maximum bitfield size for a bloom filter (which is bounded by known `max_items`/`error_rate` parameters used elsewhere in the codebase), and perform that bound check *before* allocating the output vector. Alternatively, avoid eagerly allocating `vec_len` bytes up front; instead track only the sparse `(index, value)` pairs in a bounded map/list sized by `num_filled`, and only materialize the dense `Vec<u8>` once `vec_len` has been cross-checked against the caller-supplied `num_bits` (already available in `BitField::consensus_deserialize`) so the allocation size is derived from a value that has actually been validated against the message's own semantic constraints, not just against `MAX_MESSAGE_LEN`.

### Proof of Concept
Rust test in `stackslib::util_lib::bloom` (or an HTTP-level test hitting `RPCMempoolQueryRequestHandler::try_parse_request`):
1. Construct bytes: `[BitFieldEncoding::Sparse as u8]` + `vec_len: u32 = MAX_MESSAGE_LEN - 5` (big-endian per `write_next`) + `num_filled: u32 = 1` + one entry `(idx: u32 = 0, value: u8 = 1)`. Total wire size ≈ 14 bytes.
2. Feed these bytes into `decode_bitfield(&mut &bytes[..])` (or wrap with a `num_bits` prefix and call `BitField::consensus_deserialize`).
3. Assert/observe that `vec![0u8; vec_len as usize]` inside `decode_bitfield` allocates ~16MB (measurable via a custom allocator/heap tracker or by timing/RSS delta), while the actual wire payload consumed was only ~14 bytes — demonstrating the allocation is disproportionate to `num_filled` and to bytes actually read.
4. For end-to-end confirmation, send a crafted `POST /v2/mempool/query` HTTP request with this small body through `RPCMempoolQueryRequestHandler::try_parse_request` (stackslib/src/net/api/postmempoolquery.rs:234-255) and confirm the same allocation occurs per request, then repeat across many concurrent connections to show cumulative memory growth.

### Citations

**File:** stackslib/src/util_lib/bloom.rs (L120-123)
```rust
            let vec_len: u32 = read_next(fd)?;
            if vec_len > MAX_MESSAGE_LEN.saturating_sub(5) {
                return Err(codec_error::OverflowError("vec_len is too big".into()));
            }
```

**File:** stackslib/src/util_lib/bloom.rs (L124-130)
```rust
            let num_filled: u32 = read_next(fd)?;

            if !should_use_sparse_encoding(num_filled as usize, vec_len as usize) {
                return Err(codec_error::OverflowError(
                    "Non-sparse bitfield should not use sparse encoding.".into(),
                ));
            }
```

**File:** stackslib/src/util_lib/bloom.rs (L132-132)
```rust
            let mut ret = vec![0u8; vec_len as usize];
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

**File:** stackslib/src/net/api/postmempoolquery.rs (L247-250)
```rust
        let mut body_ptr = body;
        let mempool_body = MemPoolSyncData::consensus_deserialize(&mut body_ptr)?;

        self.mempool_query = Some(mempool_body);
```
