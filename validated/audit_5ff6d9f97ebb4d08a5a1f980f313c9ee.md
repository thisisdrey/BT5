### Title
Unbounded memory allocation in `run_http_request` before size-limit check — (File: libsigner/src/http.rs, libsigner/src/session.rs)

### Summary
`StackerDBSession::get_latest_chunks` enforces a chunk-size limit (`SIGNERS_STACKERDB_CHUNK_SIZE` / `STACKERDB_MAX_CHUNK_SIZE`) only after the full HTTP response body has already been buffered in memory. The underlying `run_http_request` reads the socket to completion with `sock.read_to_end(&mut buf)` with no size cap and no respect for `Content-Length`, so a malicious/compromised RPC endpoint the signer connects to can force unbounded memory allocation on the signer process.

### Finding Description
In `run_http_request` [1](#0-0) , the client writes the request and then calls `sock.read_to_end(&mut buf)`. `read_to_end` does not stop at any declared `Content-Length` — it keeps reading and growing `buf` until the peer closes the connection or an I/O error/timeout occurs. There is no cap applied during this read.

After the full body is read, `decode_http_body` is called: for chunked transfer-encoding it enforces `MAX_MESSAGE_LEN` via `HttpChunkedTransferReader` [2](#0-1) , but for a non-chunked body (the common case, and the one a malicious server would use to bypass the chunked cap) the body bytes are simply copied wholesale via `buf.to_vec()` with no size check at all.

Only afterward, in `get_latest_chunks`, is the resulting `Vec<u8>` compared against `limit`: [3](#0-2) 
By this point the oversized body has already been fully allocated and copied into memory (once in the `read_to_end` buffer, then again in `buf[body_offset..].to_vec()`), so the post-hoc length check only discards the result — it does nothing to bound the memory that was already committed.

A malicious node that a signer is configured to poll can respond to `GET .../stackerdb/.../{slot_id}` (built by `stackerdb_get_chunk_path`) with an arbitrarily large, non-chunked body (with or without an accurate `Content-Length` header — it's irrelevant since `read_to_end` ignores it) and force the signer to allocate memory proportional to the attacker-chosen size before any check happens.

### Impact Explanation
This is a bounded-compute/memory DoS on the signer's polling path: a single oversized HTTP response from a node the signer is configured to talk to forces the signer process to allocate memory unbounded by any protocol constant, potentially causing OOM or severe resource pressure on the signer. It is trivially repeatable on every poll cycle. It affects the availability of the signer software, not correctness of consensus state.

### Likelihood Explanation
Requires a signer to be configured to connect its `StackerDBSession` to a node under attacker control (malicious or compromised RPC endpoint) — this is an explicit precondition of the question and of typical signer deployments (signers connect to a Stacks node's RPC to read/write StackerDB chunks). No secrets or privileged roles are needed; the attacker only needs to control the HTTP responses served on that connection. Cost to the attacker is minimal — one crafted HTTP response.

### Recommendation
Enforce the size limit during the read itself rather than after full buffering:
- Pass the applicable `limit` (`SIGNERS_STACKERDB_CHUNK_SIZE` or `STACKERDB_MAX_CHUNK_SIZE`, plus reasonable header overhead) into `run_http_request`/`decode_http_body` and use a bounded reader (e.g., `Read::take(limit)` around the socket, or a loop that reads in chunks and aborts once the accumulated size exceeds the limit) instead of unconditional `sock.read_to_end(&mut buf)`.
- For the non-chunked path in `decode_http_body`, apply the same cap check as the chunked path (`HttpChunkedTransferReader` already bounds via `MAX_MESSAGE_LEN`); add an equivalent bound for identity-encoded bodies.
- Reject/close the connection as soon as the cap is exceeded rather than waiting for EOF.

### Proof of Concept
Rust test in `libsigner/src/session.rs` (or `http.rs`) tests module:
1. Spin up a local `TcpListener`.
2. On accept, read the request line, then write a valid `HTTP/1.1 200 OK\r\nContent-Length: <small>\r\n\r\n` header followed by a body many times larger than `SIGNERS_STACKERDB_CHUNK_SIZE` (e.g., write in a loop indefinitely or until a large fixed size like 500MB) without ever closing the socket promptly.
3. Call `StackerDBSession::get_latest_chunks(&[0])` (with a `stackerdb_contract_id` whose name starts with `"signer"`), and use a memory-tracking allocator or process RSS sampling to assert that memory usage grows to the full oversized body size before `get_latest_chunks` returns, i.e., assert peak allocation `> SIGNERS_STACKERDB_CHUNK_SIZE` even though the final returned value is `None` (because `body_bytes.len() > limit`), confirming the check happens only after the unbounded `read_to_end` in `libsigner/src/http.rs:251` completes.

### Citations

**File:** libsigner/src/http.rs (L204-214)
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
```

**File:** libsigner/src/http.rs (L249-253)
```rust
    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
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
