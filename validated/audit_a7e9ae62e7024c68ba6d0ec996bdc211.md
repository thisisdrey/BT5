### Title
Unbounded JSON parse of StackerDB chunk uploads before per-replica `chunk_size` is enforced - (File: `stackslib/src/net/api/poststackerdbchunk.rs`)

### Summary
`RPCPostStackerDBChunkRequestHandler::try_parse_request` gates the POST `/v2/stackerdb/:principal/:contract/chunks` body only against the network-wide `MAX_MESSAGE_LEN` (~16 MB) before running `serde_json::from_slice` on the whole body. The actual per-replica upload limit (`StackerDBConfig.chunk_size`) — which contracts can and do set far lower than 16 MB (test fixtures use 256, 1024, 4096 bytes) — is only checked afterward, deep inside `StackerDBTx::try_replace_chunk` / `PeerNetwork::validate_received_chunk`. This mirrors the Moodle draft-files bug class: the enforcement point checks a generic system ceiling instead of the caller/replica-specific limit, letting unauthenticated remote requests force full parsing of oversized payloads before rejection.

### Finding Description
The HTTP handler: [1](#0-0) 
checks only `preamble.get_content_length() > MAX_MESSAGE_LEN` — a static, network-wide cap unrelated to the target contract's replica configuration — then immediately calls `serde_json::from_slice(body)` to build a `StackerDBChunkData`, i.e. it performs JSON deserialization (which includes hex-decoding the `data` field, per `stackerdb_chunk_hex_serialize`) on the full body before any signature or size validation tied to that specific StackerDB's `chunk_size`.

The correct, replica-specific size gate only exists later:
- `StackerDBTx::try_replace_chunk` checks `chunk.len() as u64 > self.config.chunk_size` as its first gate. [2](#0-1) 
- `PeerNetwork::validate_received_chunk` likewise checks `data.data.len() as u64 > config.chunk_size` first. [3](#0-2) 

Both of these are downstream of the (expensive) HTTP-layer JSON parse, and both confirm that `chunk_size` is commonly configured far below the 16 MB ceiling (tests exercise 256/1024/4096-byte replicas): [4](#0-3) [5](#0-4) 

By contrast, other recently hardened RPC endpoints (`callreadonly`, `getmapentry`) preflight the wire size against the endpoint's *actual* configured budget via a `ParseLimiter` before allocating, specifically to avoid this class of bug: [6](#0-5) 
`poststackerdbchunk.rs` was not updated to use this pattern — it still validates against the global `MAX_MESSAGE_LEN`/`STACKERDB_MAX_CHUNK_SIZE` ceiling rather than the replica's `chunk-size`, which is exactly the equality the CHANGELOG documents as having been fixed elsewhere ("Enforced StackerDB message chunk-size check against replica configuration instead of statically against `STACKERDB_MAX_CHUNK_SIZE`"): [7](#0-6) 

### Impact Explanation
Any unauthenticated remote peer can submit a POST to `/v2/stackerdb/:principal/:contract/chunks` with a body up to `MAX_MESSAGE_LEN` (~16 MB) for a contract whose replica `chunk_size` is configured much smaller (e.g. 4 KB, as in the miner/signer StackerDB test fixtures). The node fully JSON-parses (and hex-decodes) this oversized body before the size check fires and the request is rejected with `ChunkTooBig`. This forces disproportionate CPU/memory cost per request relative to the byte budget the contract actually intends to allow, and requires no valid slot signature, no known signer, and no special role — only knowledge of a real `contract_id`. This is a bounded-compute DoS vector reachable via a single unauthenticated write-style RPC call, not simple bandwidth flooding, since the amplification comes from the mismatch between the enforced ceiling and the intended per-replica limit, not from raw volume.

### Likelihood Explanation
The endpoint is public, unauthenticated, and reachable over the standard `/v2/stackerdb/...` HTTP API on any full node running the p2p/http interface. No signer keys, no admin privileges, and no protocol handshake state are required — only a syntactically valid StacksAddress/contract-name pair matching an existing StackerDB. Because `chunk_size` values well under 16 MB are already validated by the existing test suite (e.g. `test_try_replace_chunk_enforces_config_chunk_size`, `test_validate_received_chunk_rejects_oversized`) as the intended limit, exploitation is straightforward.

### Recommendation
Have `RPCPostStackerDBChunkRequestHandler::try_parse_request` look up the target contract's `StackerDBConfig.chunk_size` (or a documented per-request approximation, e.g. the network's already-registered stacker DB configs table) and reject bodies exceeding that value before calling `serde_json::from_slice`, mirroring the `ParseLimiter`/preflight approach already used by `callreadonly`/`getmapentry`. At minimum, this closes the gap between the enforced HTTP content-length ceiling and the actual per-replica chunk_size used later in `try_replace_chunk`/`validate_received_chunk`.

### Proof of Concept
1. Identify any deployed StackerDB contract with a small `chunk-size` (e.g. 4096 bytes, as used by `TEST_CONTRACT` in `stackslib/src/net/api/tests/poststackerdbchunk.rs`).
2. Send `POST /v2/stackerdb/<address>/<contract>/chunks` with a JSON body whose `data` field hex-encodes several megabytes of arbitrary bytes, with `Content-Length` under `MAX_MESSAGE_LEN` but far above the contract's `chunk-size`.
3. Observe that `try_parse_request` passes the length check (`content_length <= MAX_MESSAGE_LEN`) and performs a full `serde_json::from_slice` (parsing/hex-decoding the multi-MB payload) before `try_replace_chunk`/`validate_received_chunk` finally rejects it with `ChunkTooBig` — confirmed structurally by `test_request_fail_chunk_too_big`, which shows the request completes a full round trip (parse → handler → ack) even though the oversized chunk is ultimately rejected. [8](#0-7)

### Citations

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L67-93)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-empty body".to_string(),
            ));
        }

        if preamble.get_content_length() > MAX_MESSAGE_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: PostStackerDBChunk body is too big".to_string(),
            ));
        }

        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        let chunk: StackerDBChunkData = serde_json::from_slice(body).map_err(Error::JsonError)?;

        self.contract_identifier = Some(contract_identifier);
        self.chunk = Some(chunk);

        Ok(HttpRequestContents::new().query_string(query))
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L405-409)
```rust
    ) -> Result<(), net_error> {
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L655-666)
```rust
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/stackerdb/tests/db.rs (L759-761)
```rust
    let mut db_config = StackerDBConfig::noop();
    db_config.chunk_size = 256;
    let tx = db.tx_begin(db_config.clone()).unwrap();
```

**File:** stackslib/src/net/stackerdb/tests/sync.rs (L1213-1214)
```rust
    let mut stackerdb_config = StackerDBConfig::template();
    stackerdb_config.chunk_size = 1024;
```

**File:** stackslib/src/net/api/read_only/parse.rs (L162-171)
```rust
/// Parse a JSON body, preflighting its wire size: it approximates what parsing
/// retains, so oversized bodies are rejected before allocating. The body buffer
/// is allocated before the baseline, so the HTTP length check bounds it instead.
fn parse_json_body<T: DeserializeOwned>(body: &[u8], limiter: &ParseLimiter) -> Result<T, Error> {
    limiter.preflight(body.len() as u64)?;
    let parsed: T = serde_json::from_slice(body)
        .map_err(|e| Error::DecodeError(format!("Failed to parse JSON body: {e}")))?;
    limiter.checkpoint()?;
    Ok(parsed)
}
```

**File:** CHANGELOG.md (L151-151)
```markdown
* Enforced StackerDB message chunk-size check against replica configuration instead of statically against `STACKERDB_MAX_CHUNK_SIZE`
```

**File:** stackslib/src/net/api/tests/poststackerdbchunk.rs (L263-296)
```rust
/// dedicated `ChunkTooBig` error code.
#[test]
fn test_request_fail_chunk_too_big() {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 33333);

    let rpc_test = TestRPC::setup(function_name!());

    // The test StackerDB `TEST_CONTRACT` configures `chunk-size: u4096`.
    // Build a validly-signed chunk whose data exceeds that so the write is rejected as too big.
    let data = vec![0x01; 8192];
    let data_hash = Sha512Trunc256Sum::from_data(&data);
    let mut slot_metadata = SlotMetadata::new_unsigned(1, 1, data_hash);
    slot_metadata.sign(&rpc_test.privk1).unwrap();

    let request = StacksHttpRequest::new_post_stackerdb_chunk(
        addr.into(),
        TEST_CONTRACT_ID.clone(),
        slot_metadata.slot_id,
        slot_metadata.slot_version,
        slot_metadata.signature.clone(),
        data,
    );

    let chunk_ack = rpc_test
        .run_one(request)
        .decode_stackerdb_chunk_ack()
        .unwrap();
    assert!(!chunk_ack.accepted);
    assert_eq!(
        chunk_ack.code,
        Some(StackerDBErrorCodes::ChunkTooBig.code())
    );
    assert!(chunk_ack.reason.is_some());
}
```
