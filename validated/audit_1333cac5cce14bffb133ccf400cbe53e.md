### Title
Unbounded memory allocation in `run_http_request` before StackerDB chunk size cap is enforced - (File: libsigner/src/http.rs, libsigner/src/session.rs)

### Summary
`SignerSession::get_latest_chunks` is meant to bound the size of a fetched StackerDB chunk to `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE`, but the underlying transport call `run_http_request` reads the entire HTTP response into memory with an unbounded `sock.read_to_end(&mut buf)` before any size validation occurs. A malicious StackerDB replica (a legitimately-provisioned signer/slot holder that a `SignerSession` is directed to query) can return an arbitrarily large HTTP body, forcing the querying node to allocate the full response before the size check ever runs.

### Finding Description
The broken invariant is "bytes allocated == bytes validated as within limit." In `libsigner/src/http.rs::run_http_request`: [1](#0-0) 
the socket is read to completion via `sock.read_to_end(&mut buf)` with no length limit at all, and only afterward are `decode_http_response` and `decode_http_body` invoked on the fully-buffered `buf`. `decode_http_body` itself only enforces `MAX_MESSAGE_LEN` for the *chunked-transfer-encoding* case via `HttpChunkedTransferReader`: [2](#0-1) 
but for a plain (non-chunked) body it simply does `buf.to_vec()` with no size check at all, and even the chunked path's cap (`MAX_MESSAGE_LEN`) is unrelated to and looser than the StackerDB-specific `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` limit that `get_latest_chunks` is trying to enforce. That StackerDB-specific limit is only applied afterward in `session.rs`, comparing `body_bytes.len() > limit` on data that has already been fully allocated by `read_to_end`.

Exploit flow: a StackerDB replica that a `SignerSession::get_latest_chunks` call is directed at (any node/slot-holder in the replica set the caller queries) responds to `GET .../chunks/{slot_id}` with a Content-Length-less or arbitrarily large body (e.g., hundreds of MB to GB), with or without `Transfer-Encoding: chunked`. `run_http_request` will read the whole body into a `Vec<u8>` before any check runs, and only then will `session.rs` reject it based on `len() > limit`. The rejection happens too late to prevent the allocation.

### Impact Explanation
Each call to `get_latest_chunks` against a malicious replica can force an essentially unbounded single-response memory allocation on the querying signer node, since neither `run_http_request`'s raw socket read nor the non-chunked path of `decode_http_body` impose any cap tied to the intended per-chunk limit. Repeated queries (which occur routinely as part of normal StackerDB chunk-fetch polling) let a single malicious replica repeatedly force large allocations, which can exhaust memory and crash or degrade the querying node — an unauthenticated DoS achievable with a handful of oversized responses, matching the Critical DoS category.

### Likelihood Explanation
The only precondition is that the attacker be one of the StackerDB replica endpoints that a signer's `SignerSession` is configured/directed to query (e.g., a legitimately-provisioned but malicious signer/slot holder in the replica set) — no RPC secret, admin role, or other peer's key is required. The attack costs the attacker only sending one oversized HTTP response per query; it is fully repeatable each time the victim signer polls that replica for chunks.

### Recommendation
Enforce a hard content-length/read cap in `run_http_request` before or during reading (e.g., use a bounded reader wrapping `sock` that aborts once bytes read exceed the caller-supplied limit, or check `Content-Length` header against the limit before reading the body and stream-read with an early abort), and apply the same StackerDB-specific limit (not just `MAX_MESSAGE_LEN`) inside `decode_http_body` for both chunked and non-chunked bodies, rejecting/aborting the read as soon as the limit is exceeded rather than after full buffering.

### Proof of Concept
Rust test plan:
1. Stand up a local `TcpListener` acting as a fake StackerDB replica.
2. On accepting a connection matching the GET path used by `get_latest_chunks`, respond with `HTTP/1.1 200 OK\r\nContent-Length: <N>\r\n\r\n` followed by `N` bytes where `N` is several hundred MB (or omit `Content-Length` and just stream bytes with `Connection: close`).
3. Call `SignerSession::get_latest_chunks` (or directly `run_http_request`) against this listener.
4. Instrument the process (e.g., via `jemalloc`/`std::alloc` global allocator hook or OS RSS sampling) to assert that resident memory grows by ~`N` bytes during the `sock.read_to_end` call in `libsigner/src/http.rs`, i.e., before `get_latest_chunks`'s `body_bytes.len() > limit` check discards the result and returns `None`. [3](#0-2)

### Citations

**File:** libsigner/src/http.rs (L196-217)
```rust
/// Decode an HTTP body, given the headers.
pub fn decode_http_body(headers: &HashMap<String, String>, mut buf: &[u8]) -> io::Result<Vec<u8>> {
    let chunked = if let Some(val) = headers.get("transfer-encoding") {
        val == "chunked"
    } else {
        false
    };

    let body = if chunked {
        // chunked encoding
        let ptr = &mut buf;
        let mut fd = HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into());
        let mut decoded_body = vec![];
        fd.read_to_end(&mut decoded_body)?;
        decoded_body
    } else {
        // body is just as-is
        buf.to_vec()
    };

    Ok(body)
}
```

**File:** libsigner/src/http.rs (L246-261)
```rust
    sock.write_all(req_txt.as_bytes())?;
    sock.write_all(payload)?;

    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
    if body_offset >= buf.len() {
        // no body
        debug!("No HTTP body");
        debug!("Headers: {:?}", &headers);
        return Ok(vec![]);
    }

    decode_http_body(&headers, &buf[body_offset..]).map_err(|e| e.into())
```
