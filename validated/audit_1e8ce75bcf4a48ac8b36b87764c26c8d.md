### Title
Unbounded memory allocation reading HTTP responses in libsigner's StackerDB RPC client - (File: libsigner/src/http.rs)

### Summary
`run_http_request` in `libsigner/src/http.rs`, used by `StackerDBSession::rpc_request` in `libsigner/src/session.rs` to poll a StackerDB replica host, reads the entire HTTP response into memory with `sock.read_to_end(&mut buf)` before any header parsing or length checking is performed. There is no cap on the number of bytes accepted, so a malicious or compromised endpoint that the signer connects to can stream data indefinitely and drive the signer process's memory usage unbounded, exactly mirroring the ArgoCD `loadRepoIndex()` bug class (fetch-then-allocate with no size/time bound).

### Finding Description
`run_http_request` sends the request and then does: [1](#0-0) 
followed by header/body decoding only after the full response has already been buffered. `decode_http_response` and `decode_http_body` do parse and bound *chunked* bodies via `HttpChunkedTransferReader` with `MAX_MESSAGE_LEN`, but that bounding only kicks in once headers advertise `Transfer-Encoding: chunked` — it cannot prevent the initial `read_to_end` from growing unbounded if the remote peer simply keeps writing bytes (with or without a `Content-Length`/chunk framing that never terminates, or a header section that is never completed, or a huge, non-chunked body).

Compare this to the equivalent code in `stackslib/src/net/httpcore.rs`, which never buffers an entire response length without first knowing/bounding it: `payload_len()` is derived from `content_length`, and unknown-length (chunked) bodies are streamed through `stream_payload`/`consume_data`, which enforces `MAX_MESSAGE_LEN` incrementally rather than after a full unbounded read: [2](#0-1) 

`libsigner/src/http.rs::run_http_request` breaks this invariant: instead of reading up to a known/bounded length, it reads until EOF with no upper bound: [3](#0-2) 

This function is reached by every StackerDB RPC call the signer makes (`list_chunks`, `get_chunks`, `get_latest_chunks`, `put_chunk`), all of which funnel through `rpc_request`: [4](#0-3) 

### Impact Explanation
A remote endpoint the signer connects to over `StackerDBSession` (identified only by a configured `host:port` string, over a plain, unauthenticated TCP/HTTP connection) can respond to any of these RPC calls with an ever-growing byte stream and force the signer process to keep allocating memory until it exhausts available RAM and crashes (or is OOM-killed), i.e., unauthenticated remote Denial-of-Service — this maps to the Critical bucket ("remote crash/unauthenticated DoS from few messages"). The signer is a component whose ongoing liveness directly affects consensus availability, so crashing it via a single malformed/hostile response is a meaningful impact, not merely a bandwidth-flooding/volumetric issue: a single connection with a slow, unterminated stream (low traffic volume) is sufficient, since no byte cap exists at any point before the final in-memory `Vec<u8>` is handed off for parsing.

### Likelihood Explanation
The only bound present is `socket_timeout` (read/write timeout), which limits how long a *single read() call* may block for, but does not limit the *total number of bytes* accumulated across many successive `read()` calls into `buf` before EOF — an attacker can keep sending small chunks of data faster than the timeout to keep the connection alive indefinitely while growing `buf`. No authentication step happens before this buffering (`decode_http_response` runs only after `read_to_end` completes), so a single hostile/compromised remote party reachable at the configured host is sufficient to trigger it, making this readily reachable, analogous to the ArgoCD Helm registry scenario where a single malicious response source triggers unbounded memory growth in a fetch loop.

### Recommendation
Bound the response read the same way the P2P/RPC layer does in `stackslib/src/net/httpcore.rs`: read incrementally with a hard byte cap (e.g., `MAX_MESSAGE_LEN`, or `STACKERDB_MAX_CHUNK_SIZE` for these StackerDB endpoints) using a `BoundReader`-like wrapper (already available in `stacks-common/src/util/retry.rs`) around the socket before/while reading, rather than calling `sock.read_to_end(&mut buf)` directly. Parse headers first (with a small bounded read) to obtain `Content-Length` when present, and read exactly that many bytes (capped); for chunked/unterminated streams, abort once the configured maximum size is exceeded rather than after full buffering.

### Proof of Concept
1. Stand up a raw TCP listener acting as the "StackerDB host" that a `StackerDBSession` connects to (as in `libsigner/src/session.rs::connect_or_reconnect`).
2. When a request arrives, send a valid-looking status line/headers (or omit `Content-Length`/`Transfer-Encoding` entirely) and then continuously write bytes (e.g., in a loop, writing small buffers slower than the socket read timeout so each individual `read()` succeeds) without ever closing the connection or sending `\r\n0\r\n\r\n`.
3. Have the client call any of `list_chunks()`, `get_chunks()`, `get_latest_chunks()`, or `put_chunk()`, which internally calls `rpc_request` → `run_http_request`.
4. Observe that `buf` inside `run_http_request` (`libsigner/src/http.rs` line 249-251) grows without bound as long as the attacker keeps streaming data, consuming increasing amounts of process memory until the client OOMs or the OS kills the process — before `decode_http_response`/`decode_http_body` ever get a chance to enforce their size checks.

### Citations

**File:** libsigner/src/http.rs (L219-261)
```rust
/// Run an HTTP request, synchronously, through the given read/write handle
/// Return the HTTP reply, decoded if it was chunked
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

**File:** stackslib/src/net/httpcore.rs (L1611-1645)
```rust
    fn stream_payload<R: Read>(
        &mut self,
        preamble: &StacksHttpPreamble,
        fd: &mut R,
    ) -> Result<(Option<(StacksHttpMessage, usize)>, usize), NetError> {
        if self.payload_len(preamble).is_some() {
            return Err(NetError::InvalidState);
        }
        match preamble {
            StacksHttpPreamble::Request(_) => {
                // HTTP requests can't be chunk-encoded, so this should never be reached
                return Err(NetError::InvalidState);
            }
            StacksHttpPreamble::Response(ref http_response_preamble) => {
                if !http_response_preamble.is_chunked() {
                    return Err(NetError::InvalidState);
                }

                // sanity check -- if we're receiving a response, then we must have earlier issued
                // a request, or we must be in client mode. Thus, we must already know which
                // response handler to use. Otherwise, someone sent us malforemd data.
                if self.request_handler_index.is_none() && !self.allow_arbitrary_response {
                    self.reset();
                    return Err(NetError::DeserializeError(
                        "Unsolicited HTTP response".to_string(),
                    ));
                }

                // message of unknown length.  Buffer up and maybe we can parse it.
                let (message_bytes_opt, num_read) = self
                    .consume_data(http_response_preamble, fd)
                    .inspect_err(|_e| {
                    self.reset();
                })?;

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
