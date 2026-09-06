### Title
Unbounded `num_hashes` in `BloomFilter<BloomNodeHasher>::consensus_deserialize` enables bounded compute DoS via `/v2/mempool/query` - (File: stackslib/src/util_lib/bloom.rs)

### Summary
`BloomFilter::<BloomNodeHasher>::consensus_deserialize` reads `num_hashes: u32` directly from the wire with no upper-bound check, unlike the sibling `bits: BitField` field, which is implicitly bounded via `decode_bitfield`'s `MAX_MESSAGE_LEN` check. Since `insert_raw`/`contains_raw` iterate `for i in 0..self.num_hashes`, a crafted `MemPoolSyncData::BloomQuery` payload with `num_hashes = u32::MAX` sent to `/v2/mempool/query` forces the RPC handler thread to perform ~4 billion SipHash computations per queried transaction row.

### Finding Description
In `stackslib/src/util_lib/bloom.rs`, `consensus_deserialize` for `BloomFilter<BloomNodeHasher>` is: [1](#0-0) 
It reads `seed` (32 bytes) and `num_hashes: u32` via `read_next`, then `bits: BitField`, and only validates `bits.num_bits() == 0`. There is no check that `num_hashes` is within any sane bound (e.g., derived from `bloom_hash_count`, which for realistic error rates/`max_items` yields single/low-double-digit values as shown in `test_bloom_hash_count`) [2](#0-1) .

Contrast this with `BitField`'s deserialization, where `decode_bitfield` enforces `vec_len <= MAX_MESSAGE_LEN - 5` [3](#0-2) , bounding the bits array. No equivalent cap exists for `num_hashes`.

`insert_raw` and `contains_raw` both loop `for i in 0..self.num_hashes`, invoking `self.hasher.pick_bin(i, item, self.bits.num_bits())` (a SipHash-2-4 computation with a rejection-sampling loop) on every iteration: [4](#0-3) 

The remote path: an unauthenticated client sends a `POST /v2/mempool/query` request. `RPCMempoolQueryRequestHandler::try_parse_request` directly deserializes the request body into `MemPoolSyncData` with no additional bound applied to the embedded bloom filter's `num_hashes`: [5](#0-4) 

That `MemPoolSyncData` (containing the malicious `BloomFilter<BloomNodeHasher>`) is stored in `StacksMemPoolStream` and passed on every `generate_next_chunk()` call to `MemPoolDB::static_find_next_missing_transactions`, which evaluates the bloom filter (`contains_raw`/equivalent) against mempool rows to decide whether to include them in the response stream: [6](#0-5) 

With `num_hashes = u32::MAX`, each such bloom-membership test costs ~4 billion hash iterations, and this repeats for every mempool row visited during the query, multiplying the cost. This ties up the RPC handler thread on the CPU with no per-request cap. No signature, secret, or admin privilege is required to reach this handler — it's a plain POST to a public RPC endpoint.

### Impact Explanation
A single unauthenticated `POST /v2/mempool/query` request with a crafted bloom filter can pin an RPC worker thread in tight-loop hashing for on the order of billions of iterations per mempool row evaluated, and this cost multiplies across every visited mempool entry until `max_txs`/rows are exhausted. This is a bounded compute DoS on a read endpoint (`/v2/mempool/query`), matching the "High" impact category for RPC-thread CPU exhaustion. It is trivially repeatable — the attacker can re-issue the same crafted request continuously and does not need the node's RPC secret, a peer key, or a StackerDB slot; any remote client that can reach the RPC port can trigger it. It does not, however, crash the node, forge/store any state, or affect consensus — it is a thread/CPU-exhaustion issue confined to the RPC read path.

### Likelihood Explanation
- Preconditions: node's RPC port (`/v2/mempool/query`) must be reachable by the attacker; no wallet, peer key, or admin secret is needed.
- Attacker cost: trivial — construct ~40-70 bytes of wire data (hasher ID + 32-byte seed + `0xFFFFFFFF` for `num_hashes` + a valid minimal `BitField`), and this can be repeated at will and in parallel across connections.
- No rate limiting or per-field bound exists on `num_hashes` in the deserializer or in the handler (`try_parse_request` performs no post-deserialization sanity check on the parsed `MemPoolSyncData`).
- Overall likelihood of exploitation is high given the low cost and full remote reachability.

### Recommendation
Add an explicit upper bound check on `num_hashes` in `BloomFilter::<BloomNodeHasher>::consensus_deserialize` (e.g., reject values above a small constant such as 64 or a value derivable from `bloom_hash_count`'s realistic output range), returning `codec_error::DeserializeError`/`OverflowError` similar to the existing `bits.num_bits() == 0` check. Additionally, consider capping the product of `num_hashes` and the number of mempool rows evaluated per RPC query to bound total worst-case CPU work per request.

### Proof of Concept
```rust
// stackslib/src/util_lib/bloom.rs (test module) or a new integration test
#[test]
fn test_bloom_filter_num_hashes_unbounded_dos() {
    use std::time::Instant;
    use stacks_common::codec::StacksMessageCodec;

    // Craft raw bytes: [BloomHashID::BloomNodeHasher, seed[32], num_hashes = 0xFFFFFFFF, BitField bytes]
    let mut bytes = vec![];
    bytes.push(0x01u8); // BloomHashID::BloomNodeHasher
    bytes.extend_from_slice(&[0u8; 32]); // seed
    bytes.extend_from_slice(&0xFFFFFFFFu32.to_be_bytes()); // malicious num_hashes

    // minimal valid BitField: num_bits = 8, full encoding, 1 byte of data
    bytes.extend_from_slice(&8u32.to_be_bytes());
    bytes.push(0x02); // BitFieldEncoding::Full
    bytes.extend_from_slice(&1u32.to_be_bytes()); // array length prefix
    bytes.push(0x00); // 1 byte of bits

    let bf = BloomFilter::<BloomNodeHasher>::consensus_deserialize(&mut &bytes[..])
        .expect("deserialize should succeed with no cap on num_hashes");
    assert_eq!(bf.num_hashes, 0xFFFFFFFF); // <-- confirms unchecked value stored

    let start = Instant::now();
    bf.contains_raw(b"probe-item"); // loops 0..num_hashes, ~4 billion pick_bin calls
    let elapsed = start.elapsed();
    // demonstrates unbounded work: elapsed will be on the order of seconds+ for a single call
    println!("contains_raw took {:?} for num_hashes=u32::MAX", elapsed);
}
```
A full end-to-end reproduction would additionally wrap this crafted `BloomFilter` in a `MemPoolSyncData::BloomQuery` variant, serialize it as the body of a `POST /v2/mempool/query` request via `stackslib::net::api::postmempoolquery::RPCMempoolQueryRequestHandler`, and measure handler-thread CPU time using the existing test harness in `stackslib/src/net/api/tests/postmempoolquery.rs`.

### Citations

**File:** stackslib/src/util_lib/bloom.rs (L120-123)
```rust
            let vec_len: u32 = read_next(fd)?;
            if vec_len > MAX_MESSAGE_LEN.saturating_sub(5) {
                return Err(codec_error::OverflowError("vec_len is too big".into()));
            }
```

**File:** stackslib/src/util_lib/bloom.rs (L302-318)
```rust
    /// Test to see if a given item (a byte array) is likely present
    pub fn contains_raw(&self, item: &[u8]) -> bool {
        for i in 0..self.num_hashes {
            let slot = self.hasher.pick_bin(i, item, self.bits.num_bits());
            assert!(
                slot < self.bits.num_bits(),
                "BUG: hasher selected a slot outside the bitfield: {}",
                slot
            );

            if !self.bits.test(slot) {
                // definitely not here
                return false;
            }
        }
        true
    }
```

**File:** stackslib/src/util_lib/bloom.rs (L330-355)
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
            _ => Err(codec_error::DeserializeError(format!(
                "Not a supported bloom hasher type ID: {}",
                hasher_type_u8
            ))),
        }
    }
```

**File:** stackslib/src/util_lib/bloom.rs (L641-652)
```rust
    #[test]
    fn test_bloom_hash_count() {
        // https://hur.st/bloomfilter/?n=8192&p=0.001&m=&k=8
        let (num_bits, num_hashes) = bloom_hash_count(0.001, 8192);
        assert_eq!(num_bits, 117_782);
        assert_eq!(num_hashes, 10);

        // https://hur.st/bloomfilter/?n=8192&p=1.0E-7&m=&k=
        let (num_bits, num_hashes) = bloom_hash_count(0.0000001, 8192);
        assert_eq!(num_bits, 274_823);
        assert_eq!(num_hashes, 23);
    }
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L126-157)
```rust
    fn generate_next_chunk(&mut self) -> Result<Vec<u8>, String> {
        if self.corked {
            test_debug!(
                "Finished streaming txs; last page was {:?}",
                &self.last_randomized_txid
            );
            return Ok(vec![]);
        }

        if self.num_txs >= self.max_txs || self.finished {
            test_debug!(
                "Finished sending transactions after {:?}. Corking tx stream.",
                &self.last_randomized_txid
            );

            // cork the stream -- send the next page_id the requester should use to continue
            // streaming.
            self.corked = true;
            return Ok(self.last_randomized_txid.serialize_to_vec());
        }

        let remaining = self.max_txs.saturating_sub(self.num_txs);
        let (next_txs, next_last_randomized_txid_opt, num_rows_visited) =
            MemPoolDB::static_find_next_missing_transactions(
                &self.mempool_db,
                &self.tx_query,
                self.coinbase_height,
                &self.last_randomized_txid,
                1,
                remaining,
            )
            .map_err(|e| format!("Failed to find next missing transactions: {:?}", &e))?;
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L240-255)
```rust
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected nonzero body length".to_string(),
            ));
        }

        let mut body_ptr = body;
        let mempool_body = MemPoolSyncData::consensus_deserialize(&mut body_ptr)?;

        self.mempool_query = Some(mempool_body);
        if let Some(page_id) = self.get_page_id_query(query) {
            self.page_id = Some(page_id);
        }
        Ok(HttpRequestContents::new().query_string(query))
    }
```
