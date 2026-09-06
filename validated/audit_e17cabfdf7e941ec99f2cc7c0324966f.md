### Title
Unbounded HTTP response buffering in `libsigner` StackerDB session client enables remote memory-exhaustion DoS - (File: libsigner/src/http.rs)

### Summary
`run_http_request` in `libsigner/src/http.rs`, used by `StackerDBSession` to talk to a StackerDB-hosting Stacks node, reads the entire HTTP response into memory with `sock.read_to_end(&mut buf)` before any header or length check is applied, and the connection itself is plain, unauthenticated TCP with no TLS. This mirrors the NeuVector telemetry-sender bug class exactly: no transport authentication plus an unbounded response read, which lets whoever controls or intercepts the connection force unbounded memory allocation on the client.

### Finding Description
`StackerDBSession::rpc_request` (libsigner/src/session.rs:162-172) calls `run_http_request` to issue signer RPC requests (list/get/put chunk) against a StackerDB replica host over a raw `TcpStream` with no TLS: [1](#0-0) [2](#0-1) 

The request/response cycle is implemented in `run_http_request`: [3](#0-2) 

`sock.read_to_end(&mut buf)` has no size cap — it will keep growing `buf` for as long as the peer keeps sending bytes (or until the process runs out of memory), and only *after* all bytes have been buffered does the code call `decode_http_response` to parse headers/status. Compare this to the rest of the HTTP stack (`stackslib/src/net/httpcore.rs`, `stacks-common/src/util/chunked_encoding.rs`), which enforces `MAX_MESSAGE_LEN` both for chunked bodies (`HttpChunkedTransferReaderState::read_chunk_bytes`, which errors with `OverflowError` once `total_size >= max_size`) and for content-length-bounded bodies via `parse_raw_bytes`/`parse_bytes`: [4](#0-3) [5](#0-4) 

`decode_http_body` in the same `libsigner/src/http.rs` file does apply `MAX_MESSAGE_LEN` — but only for the `chunked` transfer-encoding branch; for a non-chunked response it just takes `buf.to_vec()` verbatim from whatever `read_to_end` already buffered: [6](#0-5) 

So the size limit that exists elsewhere in the codebase for bounding response bodies is bypassed at the point where the bytes are actually pulled off the socket in `run_http_request`. Because the transport is plaintext TCP with no certificate/identity verification, any party able to answer on that TCP connection (a compromised/misbehaving StackerDB-hosting node, or a network path attacker performing a MITM) can stream an effectively unbounded response and force the signer process into unbounded allocation — the same fault class as GHSA-qqj3-g7mx-5p4w (no transport authentication + unbounded response read).

### Impact Explanation
A single malicious or MITM'd StackerDB host response can drive the calling signer process's memory usage without bound, leading to an OOM crash/DoS of the signer with a handful of messages (one oversized/never-ending response body). This matches the "Critical – remote crash/unauthenticated DoS from few messages" bar, since no signature, authentication, or elevated privilege is required to trigger it — only network-level ability to answer (or intercept) the StackerDB RPC TCP connection the signer initiates.

### Likelihood Explanation
`StackerDBSession` is the standard transport signer binaries use to talk to a StackerDB replica; it is unauthenticated and unencrypted, so any host reachable at the configured `host:port` (or anyone able to intercept that plaintext TCP session) can serve the oversized response. No cryptographic material, node secret, or privileged role is needed — only the ability to respond on the connection the signer opens, making this readily reachable in real deployments where the StackerDB-hosting node is remote or the network path is untrusted.

### Recommendation
Bound the response the client is willing to buffer before parsing, mirroring `MAX_MESSAGE_LEN` enforcement used elsewhere in the codebase:
- In `run_http_request` (libsigner/src/http.rs), replace `sock.read_to_end(&mut buf)` with a bounded read loop (e.g., `Read::take(MAX_MESSAGE_LEN)`) that aborts once the cap is exceeded, and validate the announced `Content-Length` header against that cap before reading the body.
- Ensure the non-chunked branch of `decode_http_body` also enforces `MAX_MESSAGE_LEN` on `buf.len()`, not just the chunked branch.

### Proof of Concept
1. Stand up a TCP listener on the host:port a `StackerDBSession` is configured to contact.
2. On accepting the connection, send a valid HTTP/1.1 status line and headers (e.g. `HTTP/1.1 200 OK\r\nContent-Length: 999999999999\r\n\r\n`) followed by an endless stream of arbitrary bytes without closing the connection.
3. Observe the signer process's memory grow unbounded in `run_http_request`'s `sock.read_to_end(&mut buf)` call until the process is OOM-killed or the host machine becomes unresponsive.

### Citations

**File:** libsigner/src/session.rs (L126-136)
```rust
    /// connect or reconnect to the node
    fn connect_or_reconnect(&mut self) -> Result<(), RPCError> {
        debug!("connect to {}", &self.host);
        let sock = TcpStream::connect(&self.host)?;
        // Make sure we don't hang forever if for some reason our node does not
        // respond as expected such as failing to properly close the connection
        sock.set_read_timeout(Some(self.socket_timeout))?;
        sock.set_write_timeout(Some(self.socket_timeout))?;
        self.sock = Some(sock);
        Ok(())
    }
```

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

**File:** libsigner/src/http.rs (L246-253)
```rust
    sock.write_all(req_txt.as_bytes())?;
    sock.write_all(payload)?;

    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
```

**File:** stacks-common/src/util/chunked_encoding.rs (L185-198)
```rust
    fn read_chunk_bytes<R: Read>(&mut self, fd: &mut R, buf: &mut [u8]) -> io::Result<usize> {
        assert_eq!(self.parse_step, HttpChunkedTransferParseMode::Chunk);

        if self.total_size >= self.max_size && self.chunk_size > 0 {
            return Err(io::Error::other(ChunkedError::OverflowError(
                "HTTP body exceeds maximum expected length".to_string(),
            )));
        }

        let remaining = if self.chunk_size - self.chunk_read <= (self.max_size - self.total_size) {
            self.chunk_size - self.chunk_read
        } else {
            self.max_size - self.total_size
        };
```

**File:** stackslib/src/net/http/common.rs (L109-127)
```rust
/// Helper function to read a raw bytestream
pub fn parse_raw_bytes(
    preamble: &HttpResponsePreamble,
    body: &[u8],
    max_len: u64,
    expected_content_type: HttpContentType,
) -> Result<Vec<u8>, Error> {
    if preamble.content_type != expected_content_type {
        return Err(Error::DecodeError(format!(
            "Invalid content-type: expected {}",
            expected_content_type
        )));
    }
    let out_len = usize::try_from(max_len).unwrap().min(body.len());
    let out_bytes = body
        .get(..out_len)
        .ok_or_else(|| Error::DecodeError("Unexpected body size".into()))?;
    Ok(out_bytes.to_vec())
}
```
