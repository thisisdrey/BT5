### Title
Unbounded `sock.read_to_end` in `run_http_request` defeats the `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` post-hoc check in `get_latest_chunks` - ([File: libsigner/src/session.rs])

### Summary
`StackerDBSession::get_latest_chunks` (session.rs lines 230-262) enforces a chunk-size `limit` (2MB for `signer`-prefixed contracts, 16MB otherwise) only *after* calling `self.rpc_request`, which delegates to `run_http_request` in http.rs. `run_http_request` reads the entire HTTP response into memory via `sock.read_to_end(&mut buf)` with no size cap before any length check is applied, so a malicious node can force the signer to fully buffer an arbitrarily large response body before the `body_bytes.len() > limit` check ever runs.

### Finding Description
The broken equality: memory allocated to hold the HTTP response (`buf` in `run_http_request`) is not bounded by `limit`; it is bounded only by how much data the malicious peer chooses to send and the socket timeout. In `libsigner/src/http.rs`, lines 246-251:
```
sock.write_all(req_txt.as_bytes())?;
sock.write_all(payload)?;
let mut buf = vec![];
sock.read_to_end(&mut buf)?;
```
`read_to_end` grows `buf` without any upper bound, reading until the peer closes the connection (the request sends `Connection: close`, so the server dictates when EOF occurs). Only after this full read does control return to `StackerDBSession::rpc_request` (session.rs line 170) and then to `get_latest_chunks` (session.rs lines 240-248):
```
Ok(body_bytes) => {
    // Verify that the chunk is not too large
    if body_bytes.len() > limit {
        None
    } else {
        Some(body_bytes)
    }
}
```
By this point the oversized buffer has already been allocated and fully populated in memory; the check merely discards the reference to it and returns `None`. The `limit` variable (2MB/16MB) gives the false impression that responses are size-bounded, but it never constrains the actual read. A malicious node the signer connects to for `get_latest_chunks` (or `list_chunks`, `get_chunks`, `put_chunk`, which share `rpc_request`) can return a response body far larger than either constant — limited only by what the attacker is willing to send and the client's read timeout — forcing the signer process to allocate that much memory per request.

### Impact Explanation
A single malicious/compromised StackerDB-serving node can cause the connecting signer to allocate an attacker-chosen amount of memory per `get_latest_chunks`/`get_chunks`/`list_chunks`/`put_chunk` RPC call, with no cap enforced before allocation. This is a remote, unauthenticated memory-exhaustion DoS against the signer process, repeatable on every call the signer makes to that node (and to any other node an attacker can position). It matches the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
No privileged role or secret is required: the attacker only needs to be the node the signer is configured to query (or otherwise able to respond as the RPC server the signer connects to for its StackerDB session). The attacker fully controls the HTTP response the signer's `TcpStream` reads. Exploitation cost is low — send a large body and keep the connection open until the signer's read completes or times out.

### Recommendation
Bound the read in `run_http_request` using the `Content-Length` header (or a hard cap such as `STACKERDB_MAX_CHUNK_SIZE`/`MAX_MESSAGE_LEN`) and abort the read as soon as the received length exceeds the applicable limit, instead of buffering the full body via unbounded `read_to_end` and checking size afterward. Enforce the `limit` from `get_latest_chunks` at the transport-read layer, not after the full body has already been materialized.

### Proof of Concept
Rust test: bind a `TcpListener`, and in the accepted connection's handler write a valid HTTP header (`HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n`) followed by e.g. 100MB (or more) of arbitrary body bytes, then close the socket. Call `StackerDBSession::get_latest_chunks(&[0])` against a contract whose name starts with `signer` (so `limit = SIGNERS_STACKERDB_CHUNK_SIZE` = 2MB) pointed at this listener. Instrument/measure that `run_http_request`'s `buf` (http.rs line 249-251) grows to the full 100MB before `decode_http_response`/`decode_http_body` return, and that only afterward does `session.rs` line 243's `body_bytes.len() > limit` check discard it as `None` — demonstrating the allocation already occurred despite exceeding `limit` by ~50x. [1](#0-0) [2](#0-1)

### Citations

**File:** libsigner/src/session.rs (L230-262)
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

**File:** libsigner/src/http.rs (L221-261)
```rust
pub fn run_http_request<S: Read + Write>(
    sock: &mut S,
    host: &str,
    verb: &str,
    path: &str,
    content_type: Option<&str>,
    payload: &[u8],
) -> Result<Vec<u8>, RPCError> {
    let content_length_hdr = if !payload.is_empty() {
        format!("Content-Length: {}\r\n", payload.len())
    } else {
        "".to_string()
    };

    let req_txt = if let Some(content_type) = content_type {
        format!(
            "{verb} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Type: {content_type}\r\n{content_length_hdr}User-Agent: libsigner/0.1\r\nAccept: */*\r\n\r\n"
        )
    } else {
        format!(
            "{verb} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n{content_length_hdr}User-Agent: libsigner/0.1\r\nAccept: */*\r\n\r\n"
        )
    };
    debug!("HTTP request\n{}", &req_txt);

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
