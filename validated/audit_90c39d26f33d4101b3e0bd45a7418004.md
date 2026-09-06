### Title
Unbounded HTTP response buffering in `libsigner` StackerDB client transport before status/size validation - (File: `libsigner/src/http.rs`)

### Summary
`run_http_request` in `libsigner/src/http.rs`, used by `StackerDBSession::rpc_request` (`libsigner/src/session.rs`) to talk to a Stacks node's StackerDB RPC endpoints, reads the entire HTTP response into memory with `sock.read_to_end(&mut buf)` before any status code, `Content-Length`, or size validation occurs. This mirrors the reported bug class: a remote peer's response (success or error) is fully buffered with no upper bound, prior to the point where the code would normally reject or truncate it.

### Finding Description
`run_http_request` sends the request and then does: [1](#0-0) 

`sock.read_to_end(&mut buf)` has no size cap and will keep growing `buf` for as long as the remote side keeps sending bytes on the connection (bounded only by the configured socket read timeout, not by byte count). Only *after* this unbounded read completes does the code call `decode_http_response`, which is the first place that inspects the status code: [2](#0-1) 

Even for a non-chunked body, once headers are parsed the remaining bytes are simply copied as-is (`buf.to_vec()`), with the only cap (`MAX_MESSAGE_LEN`) applied exclusively to the chunked-encoding path via `HttpChunkedTransferReader`: [3](#0-2) 

By contrast, the main node-to-node HTTP stack in `stackslib/src/net` enforces `MAX_MESSAGE_LEN` on payload length *before* buffering the body (`stackslib/src/net/connection.rs::consume_preamble`) and bounds error-body reads via `BoundReader` (`stackslib/src/net/httpcore.rs::try_parse_error_response`). The `libsigner` HTTP client transport used for StackerDB RPC lacks this equivalent bound: nothing stops the peer at the other end of the TCP socket from streaming an arbitrarily large response (a large `Content-Length` body, or an unterminated stream) that gets fully accumulated in `buf` prior to any length check.

### Impact Explanation
`StackerDBSession` is the transport a stacks-signer uses to fetch/push StackerDB chunks and metadata (`list_chunks`, `get_chunks`, `get_latest_chunks`, `put_chunk` in `libsigner/src/session.rs`). Because `run_http_request` performs the unbounded read before validating anything about the response, a peer able to respond on this connection (a malicious/compromised counterpart, or an on-path attacker able to inject data into the TCP stream) can force the signer process to allocate memory proportional to however much data it chooses to send, with no cap enforced by this code path, until the process exhausts memory or the OS kills it. This is a bounded-by-timeout-only DoS vector on the signer's transport layer.

### Likelihood Explanation
Exploitation requires the ability to control or intercept the response on the `StackerDBSession` TCP connection (i.e., the node/host the signer is configured to talk to, or an on-path attacker on that connection, since it is unencrypted). It is not exploitable by an arbitrary unauthenticated Internet peer with no relationship to the signer's configured host, which somewhat limits reachability compared to the ideal "any remote unprivileged peer" analog.

### Recommendation
Bound the initial socket read in `run_http_request` (e.g., wrap `sock` in a size-limited reader, or loop reading in chunks while enforcing a hard maximum such as `MAX_MESSAGE_LEN`/`STACKERDB_MAX_CHUNK_SIZE` and aborting/erroring once exceeded), mirroring the bound already applied to the chunked-decoding path in `decode_http_body`, and apply the same cap uniformly to non-chunked bodies based on `Content-Length`.

### Proof of Concept
1. Point a `StackerDBSession` at a controlled TCP endpoint (or intercept the plaintext connection to the configured node host).
2. On accepting the connection, send a well-formed status line/headers followed by an indefinitely large (or very large, e.g. multi-GB) body without closing the connection promptly.
3. Observe that `run_http_request`'s `sock.read_to_end(&mut buf)` keeps growing `buf` in the signer process, consuming memory proportional to bytes sent, before `decode_http_response` ever inspects the status code.

### Citations

**File:** libsigner/src/http.rs (L134-150)
```rust
pub fn decode_http_response(payload: &[u8]) -> Result<(HashMap<String, String>, usize), RPCError> {
    // realistically, there won't be more than 32 headers
    let mut headers_buf = [httparse::EMPTY_HEADER; MAX_HTTP_HEADERS];
    let mut resp = httparse::Response::new(&mut headers_buf);

    // consume respuest
    let (headers, body_offset) =
        if let Ok(httparse::Status::Complete(body_offset)) = resp.parse(payload) {
            if let Some(code) = resp.code {
                if code != 200 {
                    return Err(RPCError::HttpError(code.into()));
                }
            } else {
                return Err(RPCError::MalformedResponse(
                    "No HTTP status code returned".to_string(),
                ));
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
