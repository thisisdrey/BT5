### Title
Unbounded `sock.read_to_end` in `run_http_request` causes memory/compute DoS on signer's StackerDB HTTP client - (File: libsigner/src/http.rs)

### Summary
`run_http_request` in `libsigner/src/http.rs` reads an HTTP response from a remote peer via `sock.read_to_end(&mut buf)` with no length cap and no read timeout, before any HTTP header parsing or `Content-Length`/`MAX_MESSAGE_LEN` enforcement occurs. A malicious or misbehaving StackerDB HTTP endpoint contacted by `StackerDBSession` (used for `get_chunks`/`get_latest_chunks` in `stacks-signer/src/client/stackerdb.rs`) can stream an arbitrarily large or arbitrarily slow response, causing the signer's request thread to block indefinitely and/or allocate unbounded memory.

### Finding Description
`run_http_request` writes the HTTP request, then calls: [1](#0-0) 

`sock.read_to_end(&mut buf)` has no size limit and no timeout — it will keep growing `buf` and blocking on `read()` until the remote side closes the connection (EOF) or the process runs out of memory. Only *after* this unbounded read completes does the code call `decode_http_response` (which parses headers) and `decode_http_body`, which does apply `MAX_MESSAGE_LEN` for chunked bodies via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`: [2](#0-1) 

Since these length/format checks are applied to an in-memory buffer already fully collected from the socket, they do nothing to bound the initial `read_to_end` call. A malicious StackerDB server the signer queries (via `StackerDBSession`, used for chunk fetches in `stacks-signer/src/client/stackerdb.rs`) can send a valid `Connection: close` response with headers and then trickle bytes slowly (or send gigabytes of data) without closing the socket, keeping the caller blocked in `read_to_end` and growing `buf` unbounded. This directly matches the claimed equality break: "bytes read from the socket before `read_to_end` returns must be bounded by Content-Length or `MAX_MESSAGE_LEN`" — there is no such bound at the `run_http_request` level.

### Impact Explanation
This ties up the signer's client-side networking thread performing `get_chunks`/`get_latest_chunks` indefinitely and can exhaust memory on the signer host, denying it the ability to fetch fresh StackerDB chunks (block proposals, signatures, etc.) in a timely manner. This is a bounded/unbounded compute-and-memory DoS on a read-path client of the signer, matching the "High - bounded compute DoS on a read endpoint" category (and could approach Critical if memory exhaustion crashes the process). It is repeatable on every request the signer sends to the same or a different malicious StackerDB host/replica.

### Likelihood Explanation
- Precondition: the signer must be configured to fetch StackerDB chunks from a host under attacker control, or an attacker-controlled/MITM'd network path to a legitimate StackerDB HTTP endpoint. This is plausible since replica lists / stacker-db configuration can include multiple hosts and the signer client connects out to them.
- No secret or privileged role is needed by the malicious server — it just needs to accept the incoming TCP connection and respond slowly/with excess bytes.
- Attacker cost is trivial (a simple TCP server that never closes the connection or drips bytes).
- The bug is 100% reachable any time `StackerDBSession` issues an RPC to a slow/malicious server; it does not depend on race conditions or specific chain state.

### Recommendation
Add a bound to the read step in `run_http_request`:
- Read headers only up to a fixed cap (e.g., `MAX_HTTP_HEADER_LEN`/`MAX_HTTP_HEADERS`-derived cap) first, then parse `Content-Length` (or detect chunked encoding) and use a bounded reader (e.g., `Read::take(content_length)` or `HttpChunkedTransferReader` with `MAX_MESSAGE_LEN`) for the body, instead of `read_to_end` on an unconstrained buffer.
- Enforce a read/connect timeout on the underlying socket (`set_read_timeout`) so a peer that stops sending data cannot hang the thread forever.
- Cap total response size to `MAX_MESSAGE_LEN` (or a StackerDB-chunk-appropriate bound like `STACKERDB_MAX_CHUNK_SIZE` plus header overhead) and abort/error out if exceeded.

### Proof of Concept
Rust test plan (net test, no privileged access needed):
1. Spin up a `TcpListener` on localhost in a test thread.
2. On accept, read the HTTP request line/headers, then write a valid status line + headers (`HTTP/1.1 200 OK\r\nContent-Length: 999999999\r\n\r\n`) but only send a few bytes of body, then sleep/loop writing 1 byte every few seconds without ever closing the socket (or write until several hundred MB have been sent).
3. Call `run_http_request(&mut socket, ...)` from the client side (as `StackerDBSession` would).
4. Assert that `run_http_request` either returns within a bounded time (e.g., a few seconds) with an error, or that memory usage of the test process stays bounded — under the current implementation, the call will hang at `sock.read_to_end(&mut buf)` at `libsigner/src/http.rs:251` indefinitely (or grow `buf` without bound), demonstrating the missing cap/timeout.

### Citations

**File:** libsigner/src/http.rs (L204-217)
```rust
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

**File:** libsigner/src/http.rs (L249-253)
```rust
    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
```
