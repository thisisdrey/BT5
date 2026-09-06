### Title
Unbounded `sock.read_to_end` in `run_http_request` allows a malicious StackerDB replica endpoint to exhaust signer client memory before any chunk-size check - ([File: libsigner/src/http.rs])

### Summary
`run_http_request` reads the entire HTTP response into memory with `sock.read_to_end(&mut buf)` before any length validation occurs. The chunk-size limit (`SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE`) is only checked in `StackerDBSession::get_latest_chunks` after the full response has already been buffered, so a malicious StackerDB replica endpoint can stream an unbounded amount of data to the signer and force unbounded memory allocation before rejection.

### Finding Description
`run_http_request` in [1](#0-0)  writes the request and then calls `sock.read_to_end(&mut buf)`. This call has no size cap whatsoever — it keeps growing `buf` until the connection is closed by the peer or an I/O error occurs, completely independent of any `Content-Length` header or declared body size. Only after the read completes does `decode_http_response` parse headers and `decode_http_body` extract the body [2](#0-1) .

The size check that the caller relies on happens even later, in `StackerDBSession::get_latest_chunks`:
```rust
Ok(body_bytes) => {
    // Verify that the chunk is not too large
    if body_bytes.len() > limit {
        None
    } else {
        Some(body_bytes)
    }
}
``` [3](#0-2) 

This check is purely post-hoc: by the time `body_bytes.len()` is compared to `limit`, the full oversized buffer has already been allocated and copied into memory by `read_to_end`. There is no equivalent of a bounded reader (e.g., a `read_next_at_most`/`Take`-limited reader) applied during the socket read itself. Note that `decode_http_body`'s chunked-encoding path does bound decoding via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())` [4](#0-3) , but that bound only applies to the *decoding* of an already-fully-buffered `buf`; the raw socket read into `buf` at line 251 has no bound at all, chunked or not.

An attacker who controls (or can otherwise respond on) the far end of the TCP connection that `StackerDBSession::connect_or_reconnect`/`rpc_request` dials — the "StackerDB replica host" configured in `self.host` — can simply keep writing bytes on the socket for as long as the read timeout allows, regardless of what `Content-Length` (if any) it declares, causing `buf` to grow without bound until the process runs out of memory or the OS read timeout / socket timeout fires.

### Impact Explanation
A malicious or MITM'd StackerDB replica endpoint can cause the signer process to allocate memory unboundedly for a single connect-and-respond cycle, which can crash the signer via out-of-memory or severely degrade it before the socket read timeout intervenes. This is a client-side memory-exhaustion DoS against the signer process that queries the StackerDB replica.

### Likelihood Explanation
This requires the signer to be configured to talk to (or be MITM'd against) a StackerDB replica host that is attacker-controlled — i.e. the far end of the outbound HTTP connection made by `StackerDBSession`. Given that precondition, the attack is cheap and trivially repeatable: the attacker simply keeps streaming bytes on every accepted connection for up to `socket_timeout`, with no authentication or state requirements.

### Recommendation
Bound the read in `run_http_request` (e.g., wrap `sock` in a limited/`Take`-style reader capped at `MAX_MESSAGE_LEN` or the larger of `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` plus a small header allowance) before calling `read_to_end`, or read headers first, extract `Content-Length`, and then read exactly that many bytes (capped at a sane maximum) rather than reading until EOF.

### Proof of Concept
Stand up a `TcpListener` in a test, accept a connection, and after reading the request, respond with `HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n` followed by an unbounded loop writing gigabytes of filler bytes without ever closing the socket (or only closing after writing far more than `Content-Length` declared). Call `run_http_request` (or `StackerDBSession::get_latest_chunks`) against this listener and observe that `buf` in `run_http_request` grows to the full streamed size before `decode_http_response`/the `body_bytes.len() > limit` check ever executes — i.e. assert that process memory usage / `buf.capacity()` grows unbounded and unrelated to the declared `Content-Length`, demonstrating the read is not bounded prior to the length check.

### Citations

**File:** libsigner/src/http.rs (L204-210)
```rust
    let body = if chunked {
        // chunked encoding
        let ptr = &mut buf;
        let mut fd = HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into());
        let mut decoded_body = vec![];
        fd.read_to_end(&mut decoded_body)?;
        decoded_body
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

**File:** libsigner/src/session.rs (L240-248)
```rust
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
