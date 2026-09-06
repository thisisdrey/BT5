### Title
`StackerDBSession::get_chunks()` accepts unbounded chunk payload with no size check - ([File: libsigner/src/session.rs])

### Summary
`StackerDBSession::get_chunks()` stores the raw `body_bytes` returned by `rpc_request()` directly into `payloads` with no length check, unlike its sibling `get_latest_chunks()` which explicitly bounds the body against `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE`. A malicious or compromised StackerDB replica host (or a MITM on that connection) can return an oversized HTTP response body for a `GET /chunk/<slot>/<version>` request and have that payload accepted unchecked by the signer client.

### Finding Description
`get_chunks()` at [1](#0-0)  issues one `rpc_request("GET", &path, ...)` per `(slot_id, slot_version)` pair and pushes `Ok(body_bytes)` straight into `payloads` with no size validation: [2](#0-1) 

Contrast this with `get_latest_chunks()`, which computes a `limit` (`SIGNERS_STACKERDB_CHUNK_SIZE` or `STACKERDB_MAX_CHUNK_SIZE`) and discards (returns `None` for) any body exceeding it: [3](#0-2) 

Tracing the transport path: `rpc_request` calls `run_http_request`, which does `sock.read_to_end(&mut buf)` — reading the entire response until the peer closes the connection, with **no length cap at all** — then calls `decode_http_body`: [4](#0-3) 

`decode_http_body` only bounds the size via `MAX_MESSAGE_LEN` when the response uses **chunked transfer-encoding**; for a plain (non-chunked) response it just does `buf.to_vec()` with no bound whatsoever: [5](#0-4) 

So the actual guard is weaker than the question assumes: it's not that `decode_http_body` caps things at `MAX_MESSAGE_LEN` for both cases — a non-chunked reply is entirely uncapped up to whatever `read_to_end` will accumulate in memory. The only mitigation for `get_chunks()`'s callers is the per-function `limit` check that exists in `get_latest_chunks()` but is absent from `get_chunks()`. Since `get_chunks()` has no equivalent check, a hostile StackerDB host answering a `get_chunks()` request can return an arbitrarily large body (bounded only by the attacker's willingness to send bytes and the process's available memory) and it will be accepted into `payloads` as `Some(body_bytes)` unchecked.

### Impact Explanation
A malicious/compromised StackerDB replica (or on-path MITM without TLS integrity, since this is plain HTTP over TCP) that a signer talks to via `get_chunks()`/`get_chunk()` can force the signer process to allocate and hold an oversized buffer per request, and this is repeatable for every call. This is a bounded compute/memory DoS against the signer's own chunk-fetch client path, consistent with the size-guard the sibling function `get_latest_chunks()` already implements but `get_chunks()` omits. It does not corrupt consensus state, forge chunks, or bypass authentication — the caller must independently validate/deserialize any consumed data (e.g. via `get_latest`, which does length-implicit deserialization checks through `StacksMessageCodec`, though `get_chunks()` itself has no direct consumer shown here doing consensus deserialization) — so severity is bounded to a resource-exhaustion issue on the signer's outbound HTTP client rather than a network-wide propagation or write vulnerability.

### Likelihood Explanation
Precondition: the signer must be configured to call `get_chunks()`/`get_chunk()` against a StackerDB host that is malicious or is being MITM'd (no TLS, no response integrity check other than the eventual application-level signature check on decoded chunk content, which happens after the oversized allocation). Attacker cost is a single crafted oversized HTTP response; no secrets, no privileged role, and no consensus-level access are required — only the ability to answer the TCP connection the signer initiates to that host. This is repeatable on every `get_chunks()` call.

### Recommendation
Add the same size-bounding logic used in `get_latest_chunks()` to `get_chunks()`: compute the applicable `limit` (`SIGNERS_STACKERDB_CHUNK_SIZE` for signer contracts, else `STACKERDB_MAX_CHUNK_SIZE`) and reject/null out any `body_bytes` exceeding it before storing into `payloads`. Additionally, `decode_http_body`/`run_http_request` should enforce an upper bound on the number of bytes read via `read_to_end` (e.g., cap at `MAX_MESSAGE_LEN`) for both chunked and non-chunked bodies, since currently only the chunked path is bounded.

### Proof of Concept
Rust test plan (mirroring the existing `mod tests` in `libsigner/src/session.rs`):
1. Spin up a `TcpListener` mock server that, on receiving the GET request for `stackerdb_get_chunk_path(...)`, writes back a valid HTTP/1.1 200 response with `Content-Length` header and a body of size `MAX_MESSAGE_LEN - 1` (non-chunked).
2. Call `StackerDBSession::get_chunks(&[(slot_id, version)])`.
3. Assert `Ok(vec![Some(body)])` is returned with `body.len() == MAX_MESSAGE_LEN - 1`, i.e., no size check rejected/truncated it.
4. As a control, repeat the same oversized-body server response but call `get_latest_chunks(&[slot_id])` instead, and assert it returns `Ok(vec![None])` because `body_bytes.len() > limit`, demonstrating the guard exists there but not in `get_chunks()`.

### Citations

**File:** libsigner/src/session.rs (L200-227)
```rust
    /// query the replica for zero or more chunks
    fn get_chunks(
        &mut self,
        slots_and_versions: &[(u32, u32)],
    ) -> Result<Vec<Option<Vec<u8>>>, RPCError> {
        let mut payloads = vec![];
        for (slot_id, slot_version) in slots_and_versions.iter() {
            let path = stackerdb_get_chunk_path(
                self.stackerdb_contract_id.clone(),
                *slot_id,
                Some(*slot_version),
            );
            let chunk = match self.rpc_request("GET", &path, None, &[]) {
                Ok(body_bytes) => Some(body_bytes),
                Err(RPCError::HttpError(code)) => {
                    if code != 404 {
                        return Err(RPCError::HttpError(code));
                    }
                    None
                }
                Err(e) => {
                    return Err(e);
                }
            };
            payloads.push(chunk);
        }
        Ok(payloads)
    }
```

**File:** libsigner/src/session.rs (L230-248)
```rust
    fn get_latest_chunks(&mut self, slot_ids: &[u32]) -> Result<Vec<Option<Vec<u8>>>, RPCError> {
        let mut payloads = vec![];
        let limit = if self.stackerdb_contract_id.name.starts_with("signer") {
            SIGNERS_STACKERDB_CHUNK_SIZE
        } else {
            usize::try_from(STACKERDB_MAX_CHUNK_SIZE)
                .expect("infallible: StackerDB chunk size exceeds usize::MAX")
        };
        for slot_id in slot_ids.iter() {
            let path = stackerdb_get_chunk_path(self.stackerdb_contract_id.clone(), *slot_id, None);
            let chunk = match self.rpc_request("GET", &path, None, &[]) {
                Ok(body_bytes) => {
                    // Verify that the chunk is not too large
                    if body_bytes.len() > limit {
                        None
                    } else {
                        Some(body_bytes)
                    }
                }
```

**File:** libsigner/src/http.rs (L197-217)
```rust
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
