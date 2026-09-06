### Title
Unbounded `MemPoolSyncData::TxTags` tag-count triggers oversized allocation before length validation - ([File: stackslib/src/net/api/postmempoolquery.rs])

### Summary
`RPCMempoolQueryRequestHandler::try_parse_request` only rejects requests with a zero `Content-Length` and otherwise passes the raw request body directly into `MemPoolSyncData::consensus_deserialize` with no upper-bound check on body size or on any length prefix contained within it, unlike other RPC handlers that enforce `MAX_PAYLOAD_LEN`/`MAX_MESSAGE_LEN` caps. Because the `TxTags` variant embeds a wire-controlled `Vec<TxTag>` length prefix, a small POST body can declare an enormous element count for the codec to honor.

### Finding Description
In `stackslib/src/net/api/postmempoolquery.rs`, the request-decoding path is: [1](#0-0) 

The only guard is `preamble.get_content_length() == 0`; there is no check against `MAX_PAYLOAD_LEN`/`MAX_MESSAGE_LEN` before `body` (already fully buffered by the HTTP layer up to whatever size limit that layer enforces) is handed to `MemPoolSyncData::consensus_deserialize`. This differs from the pattern used in other handlers in this codebase (e.g., response-side decoding in the same file explicitly calls `parse_bytes(preamble, body, MAX_MESSAGE_LEN.into())` before trusting body content): [2](#0-1) 

`MemPoolSyncData` is defined in `stackslib/src/core/mempool.rs`, and its `TxTags` variant carries a `Vec<TxTag>` whose length is read from a wire-controlled prefix. Whether this specific decode path is actually exploitable depends on how the generic `Vec<T>::consensus_deserialize` implementation (in the `stacks_common::codec` crate) handles the declared count — specifically, whether it pre-allocates capacity (`Vec::with_capacity(len)`) proportional to the untrusted `len` before reading any element bytes, or whether it grows the vector incrementally via `push` (which would naturally bound allocation to the bytes actually available, since each element read would fail with an EOF/`CodecError` once the underlying `&[u8]` slice is exhausted).

I was not able to retrieve and inspect the body of `MemPoolSyncData`'s `consensus_deserialize` implementation nor the generic `Vec<T>` codec implementation within the available tool budget, so I cannot confirm with certainty whether the vector is pre-allocated from the untrusted count (which would reproduce the claimed over-allocation/DoS) or whether it is safely bounded by incremental reads that fail on EOF.

### Impact Explanation
If the underlying codec pre-allocates `Vec::with_capacity(len)` using the attacker-declared `TxTags` count without comparing it against the remaining buffer length, a single small POST to the always-open, unauthenticated `/v2/mempool/query` endpoint could force a huge allocation attempt, likely aborting the node process (Rust's default allocator aborts on allocation failure rather than returning a recoverable error), which is a Critical unauthenticated single-message crash. If instead the codec grows the vector incrementally and fails on the first out-of-bounds read, this finding does not hold and there is no vulnerability.

### Likelihood Explanation
The endpoint is confirmed reachable by any remote, unauthenticated client with no privileged role, and the only confirmed guard is a `Content-Length != 0` check, with no upper bound before deserialization. However, exploitability hinges entirely on the internal behavior of the generic codec's `Vec<T>` deserialization, which I could not verify in this session.

### Recommendation
Regardless of the underlying `Vec<T>` codec implementation, add a defensive upper bound in `RPCMempoolQueryRequestHandler::try_parse_request` (e.g., enforce `preamble.get_content_length() <= MAX_PAYLOAD_LEN`/`MAX_MESSAGE_LEN`) before calling `consensus_deserialize`, matching the pattern used elsewhere in the codebase. Additionally, confirm/harden the generic `Vec<T>::consensus_deserialize` in `stacks_common::codec` to bound-check the declared element count against the remaining reader length (or avoid `with_capacity` on untrusted input) before allocating.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postmempoolquery.rs` that constructs raw bytes for `MemPoolSyncData::TxTags` with a 32-byte seed followed by a `u32` tag-count of `u32::MAX` and only a few actual trailing bytes, then calls `MemPoolSyncData::consensus_deserialize` directly on a cursor over that buffer, asserting the call returns `Err(CodecError::...)` promptly rather than hanging, aborting the process, or attempting a multi-gigabyte allocation. This must be run/observed (e.g., under a memory-limited sandbox or with an allocation-tracking harness) to confirm whether a large allocation is attempted before the read fails — a detail that could not be confirmed from static inspection alone within this session.

### Citations

**File:** stackslib/src/net/api/postmempoolquery.rs (L241-248)
```rust
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected nonzero body length".to_string(),
            ));
        }

        let mut body_ptr = body;
        let mempool_body = MemPoolSyncData::consensus_deserialize(&mut body_ptr)?;
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L323-330)
```rust
    fn try_parse_response(
        &self,
        preamble: &HttpResponsePreamble,
        body: &[u8],
    ) -> Result<HttpResponsePayload, Error> {
        let bytes = parse_bytes(preamble, body, MAX_MESSAGE_LEN.into())?;
        Ok(HttpResponsePayload::Bytes(bytes))
    }
```
