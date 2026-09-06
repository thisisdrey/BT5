## Title
Unbounded HTTP response body read causes memory-exhaustion DoS in `libsigner` StackerDB client - (File: `libsigner/src/http.rs`)

### Summary
`libsigner`'s synchronous HTTP client, used by the signer's `StackerDBSession` to talk to a Stacks node's StackerDB HTTP endpoint, reads the entire raw HTTP response into memory with `sock.read_to_end(&mut buf)` before any length checking is performed. There is no cap on the number of bytes read from the socket, so a malicious or compromised StackerDB replica endpoint (or a man-in-the-middle on that connection) can stream an unbounded response body and force the signer process to allocate unbounded memory, leading to an `OutOfMemoryError`-style crash. This is the same bug class as GHSA-vc24-j8c5-2vw4 (unbounded HTTP response body read in `OpenTelemetry.Resources.Azure`).

### Finding Description
`run_http_request` in `libsigner/src/http.rs` performs the request and then reads the full response with no bound: [1](#0-0) 

Compare this with every other HTTP body-decoding path in this codebase, which does enforce a maximum size:
- `stackslib/src/net/httpcore.rs`'s `StacksHttpRecvStream`/`HttpChunkedTransferReaderState` bound chunked decoding by `MAX_MESSAGE_LEN` [2](#0-1) 
- `stacks-common/src/util/chunked_encoding.rs`'s `read_chunk_bytes` enforces `self.max_size` and errors with `ChunkedError::OverflowError` when the body exceeds the expected length [3](#0-2) 
- Even `libsigner`'s own `decode_http_body` passes `MAX_MESSAGE_LEN` into the chunked reader for the *chunked* case [4](#0-3) 

However, `run_http_request` reads the **raw socket bytes** into `buf` via `read_to_end` *before* `decode_http_response`/`decode_http_body` are even invoked, so the bound applied later inside `decode_http_body` for chunked bodies is irrelevant — the unbounded allocation already happened in the initial `read_to_end` call. For a non-chunked response there is no length check on `buf` at all; the loop only terminates once the peer closes the connection or the `socket_timeout` is hit, and until then `buf` grows without limit.

This function is called from `StackerDBSession::rpc_request`, which is used for all signer-to-node/replica StackerDB operations (`list_chunks`, `get_chunks`, `get_latest_chunks`, `put_chunk`): [5](#0-4) 

The socket read/write timeouts (`socket_timeout`) bound how long a single `read()` call can block, but they do **not** bound the total number of bytes accumulated across many successful (non-blocking) `read()` calls returning data. A peer that continuously sends data (or a slow, endless response body) will keep `read_to_end` making progress and appending to `buf` indefinitely, well past any of the size constants (`MAX_MESSAGE_LEN`, `STACKERDB_MAX_CHUNK_SIZE`, `SIGNERS_STACKERDB_CHUNK_SIZE`) used elsewhere in the codebase to police these exact same requests.

### Impact Explanation
`StackerDBSession` is the transport the signer uses to fetch/post StackerDB chunks from/to the configured node. Any endpoint capable of responding to this session's request — a compromised or malicious node the signer is pointed at, or a network position able to MITM this connection — can trigger unbounded memory growth in the signer process, exhausting available memory and crashing it (denial of service). This directly breaks the "bytes vs length" trust equality: the client trusts that the wire will terminate near the advertised/expected chunk size, but no cap is enforced on the raw bytes accumulated before any size validation runs.

### Likelihood Explanation
Requires either a malicious/compromised endpoint that a signer session is configured to talk to, or MITM capability on that link — matching the same "mitigating factor" profile as the referenced advisory. No authentication or special node state is needed once such positioning exists; a single malicious response is sufficient.

### Recommendation
Bound the initial socket read in `run_http_request` (e.g., using `BoundReader`/`HttpChunkedTransferReader` with a fixed cap such as `MAX_MESSAGE_LEN` or the StackerDB chunk size limits) instead of calling `sock.read_to_end` on an unbounded `Vec<u8>`. Abort/error out once the accumulated bytes exceed the expected maximum for the given request type (metadata list, chunk fetch, or chunk-ack).

### Proof of Concept
1. Configure a `StackerDBSession` to point at an attacker-controlled TCP endpoint (playing the role of the "node"/StackerDB replica).
2. Have the signer call any of `list_chunks`/`get_chunks`/`get_latest_chunks`/`put_chunk`, which invoke `rpc_request` → `run_http_request` (`libsigner/src/session.rs:169-171`, `libsigner/src/http.rs:221-262`).
3. From the attacker endpoint, after receiving the request, respond with a valid-looking status line/headers followed by an endless stream of body bytes (or a very large `Content-Length`-less/`Connection: keep-alive` body) that never signals EOF, sending data steadily faster than the timeout window.
4. `sock.read_to_end(&mut buf)` in `run_http_request` keeps appending to `buf` without any size cap, growing memory usage until the signer process exhausts available memory and is killed/OOMs.

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

**File:** libsigner/src/http.rs (L246-262)
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
}
```

**File:** stackslib/src/net/httpcore.rs (L855-862)
```rust
impl StacksHttpRecvStream {
    pub fn new(max_size: u64) -> StacksHttpRecvStream {
        StacksHttpRecvStream {
            state: HttpChunkedTransferReaderState::new(max_size),
            data: vec![],
            total_consumed: 0,
        }
    }
```

**File:** stacks-common/src/util/chunked_encoding.rs (L188-192)
```rust
        if self.total_size >= self.max_size && self.chunk_size > 0 {
            return Err(io::Error::other(ChunkedError::OverflowError(
                "HTTP body exceeds maximum expected length".to_string(),
            )));
        }
```

**File:** libsigner/src/session.rs (L160-172)
```rust
    /// send an HTTP RPC request and receive a reply.
    /// Return the HTTP reply, decoded if it was chunked
    fn rpc_request(
        &mut self,
        verb: &str,
        path: &str,
        content_type: Option<&str>,
        payload: &[u8],
    ) -> Result<Vec<u8>, RPCError> {
        self.with_socket(|session, sock| {
            run_http_request(sock, &session.host, verb, path, content_type, payload)
        })?
    }
```
