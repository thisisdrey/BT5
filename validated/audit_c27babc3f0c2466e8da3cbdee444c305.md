### Title
HTTP header-boundary parser disagrees with strict `\r\n\r\n` preamble scanner, enabling request-smuggling-style desync - (File: stacks-common/src/deps_common/httparse/mod.rs)

### Summary
The Stacks node's HTTP preamble handling uses two independent mechanisms to decide "where do the headers end": a lenient token-based parser (`httparse`, vendored in `stacks-common/src/deps_common/httparse/mod.rs`) that terminates the request line and the header block on a bare `\n` as well as `\r\n`, and a strict byte-scanner (`read_to_crlf2` / `body_start_search_window`) that only recognizes the exact 4-byte sequence `\r\n\r\n` as the boundary between headers and body. These two boundary definitions can disagree on crafted input, mirroring the CWE-444 bug class in the Undertow advisory (ambiguous header-block terminator causing framing desync).

### Finding Description
`Request::parse` / `Response::parse` in [1](#0-0)  call the `newline!` macro to end the request/status line, which explicitly accepts either `\r\n` or a bare `\n`: [2](#0-1) 

The header-block terminator is equally lenient: in `parse_headers_iter`, a bare `\n` on its own (not preceded by a matching `\r`) is sufficient to end the whole header section, exactly like `\r\n` would: [3](#0-2) 

Meanwhile, the outer framing code that decides how many bytes constitute the "preamble" (and therefore where the body starts) only recognizes the strict literal byte sequence `[0x0d, 0x0a, 0x0d, 0x0a]`:
- `read_to_crlf2`, used by both `HttpRequestPreamble::consensus_deserialize` and `HttpResponsePreamble::consensus_deserialize`: [4](#0-3) 
- `StacksHttp::read_preamble`, which independently scans for the same strict 4-byte window before invoking the codec: [5](#0-4) 

Because `consensus_deserialize` reads the *entire* strictly-delimited chunk into `buf_read` and then hands it to `httparse`, but only checks `Status::Complete(_)` without validating that the returned `body_offset` equals `buf_read.len()`: [6](#0-5)  and [7](#0-6) , any bytes between the lenient parser's (earlier) header-end point and the strict scanner's (later) `\r\n\r\n` are silently consumed from the socket and dropped — they are neither exposed as headers (httparse already stopped collecting them) nor treated as body (the framing layer's body offset is anchored to the later, strict boundary). Given httparse's leniency (`\r\n\n`, `\n\r\n`, or `\n\n` all validly terminate the header block), an attacker can construct a byte stream where the Stacks node's HTTP parser and any front-end intermediary using strict CRLF semantics disagree about the extent of the header block/request boundary — the classic precondition for HTTP request smuggling (CWE-444): the two components no longer agree on the same equality ("bytes consumed as headers by parser" == "bytes consumed as headers by framing").

### Impact Explanation
If the node's RPC HTTP endpoint sits behind any HTTP-aware intermediary (load balancer, reverse proxy) that enforces strict `\r\n\r\n`/CRLF semantics, this boundary disagreement lets an attacker smuggle attacker-controlled bytes that the proxy considers part of one request's body/headers but that the node's own header parser silently discards or misattributes, poisoning connection framing on keep-alive connections. This matches the report's "request smuggling" impact category (Critical tier per the task's impact list). Even without a fronting proxy, silently dropping headers located after an injected bare-LF-LF sequence (e.g., `Authorization`, `Host`, `Content-Length`) without error is itself a parser-consistency violation that could cause a request to be processed with different metadata than what any external observer (WAF, log correlator, other component parsing the same bytes) would infer.

### Likelihood Explanation
Reaching this code path requires only sending a raw, unauthenticated TCP/HTTP payload to the node's RPC listener — no credentials, keys, or special privileges are needed, and the parser accepts and continues to "successfully" build a preamble rather than rejecting the malformed sequence. Because the mismatch depends on the deployment topology (a strict fronting proxy) to be exploited as classic smuggling, likelihood is moderate but the underlying parser inconsistency itself is trivially and remotely triggerable.

### Recommendation
Make the header-terminator semantics used by `stacks-common/src/deps_common/httparse` consistent with the byte-scanners in `stackslib/src/net/http/response.rs` and `stackslib/src/net/httpcore.rs`: require canonical `\r\n\r\n` (and `\r\n` line endings generally) and reject bare `\n`/`\n\n` as valid terminators, per RFC 7230's strict recommendation to reduce ambiguity. Additionally, after calling `req.parse()`/`resp.parse()`, assert that the returned `body_offset` equals `buf_read.len()`; if not, treat it as a malformed/ambiguous request and reject it rather than silently discarding the trailing bytes.

### Proof of Concept
Send to the node's HTTP RPC port a request such as:
```
GET / HTTP/1.1\r\nHost: victim\r\n\nX-Smuggled: 1\r\nContent-Length: 0\r\n\r\n
```
- `read_to_crlf2` (stackslib/src/net/http/response.rs:359) will keep reading until it sees the final `\r\n\r\n`, consuming the whole buffer above as the "preamble."
- `httparse`'s `parse_headers_iter` (stacks-common/src/deps_common/httparse/mod.rs:713-722) will stop collecting headers at the bare `\n` right after `Host: victim\r\n`, so `X-Smuggled` and the second `Content-Length` header are silently dropped from `req.headers`, yet those bytes have already been consumed as part of the "preamble" by the strict scanner.
- Any front-end component that parses the same bytes with strict CRLF semantics (treating the bare `\n` as inert whitespace inside a header value, not a terminator) would compute a different header set / body boundary than the Stacks node, producing the framing disagreement needed for request smuggling.

### Citations

**File:** stacks-common/src/deps_common/httparse/mod.rs (L78-91)
```rust
macro_rules! newline {
    ($bytes:ident) => ({
        match next!($bytes) {
            b'\r' => {
                expect!($bytes.next() == b'\n' => Err(Error::NewLine));
                $bytes.slice();
            },
            b'\n' => {
                $bytes.slice();
            },
            _ => return Err(Error::NewLine)
        }
    })
}
```

**File:** stacks-common/src/deps_common/httparse/mod.rs (L457-471)
```rust
    /// Try to parse a buffer of bytes into the Request.
    pub fn parse(&mut self, buf: &'b [u8]) -> Result<usize> {
        let orig_len = buf.len();
        let mut bytes = Bytes::new(buf);
        complete!(skip_empty_lines(&mut bytes));
        self.method = Some(complete!(parse_token(&mut bytes)));
        self.path = Some(complete!(parse_uri(&mut bytes)));
        self.version = Some(complete!(parse_version(&mut bytes)));
        newline!(bytes);

        let len = orig_len - bytes.len();
        let headers_len = complete!(parse_headers_iter(&mut self.headers, &mut bytes));

        Ok(Status::Complete(len + headers_len))
    }
```

**File:** stacks-common/src/deps_common/httparse/mod.rs (L713-722)
```rust
        'headers: loop {
            // a newline here means the head is over!
            let b = next!(bytes);
            if b == b'\r' {
                expect!(bytes.next() == b'\n' => Err(Error::NewLine));
                result = Ok(Status::Complete(count + bytes.pos()));
                break;
            } else if b == b'\n' {
                result = Ok(Status::Complete(count + bytes.pos()));
                break;
```

**File:** stackslib/src/net/http/response.rs (L356-374)
```rust
/// Read from a stream until we see '\r\n\r\n', with the purpose of reading an HTTP preamble.
/// It's gonna be important here that R does some bufferring, since this reads byte by byte.
/// EOF if we read 0 bytes.
pub fn read_to_crlf2<R: Read>(fd: &mut R) -> Result<Vec<u8>, CodecError> {
    let mut ret = Vec::with_capacity(HTTP_PREAMBLE_MAX_ENCODED_SIZE as usize);
    while ret.len() < HTTP_PREAMBLE_MAX_ENCODED_SIZE as usize {
        let mut b = [0u8];
        fd.read_exact(&mut b).map_err(CodecError::ReadError)?;
        ret.push(b[0]);

        if let Some(last_4) = ret.last_chunk::<4>() {
            // '\r\n\r\n' is [0x0d, 0x0a, 0x0d, 0x0a]
            if last_4 == &[0x0d, 0x0a, 0x0d, 0x0a] {
                break;
            }
        }
    }
    Ok(ret)
}
```

**File:** stackslib/src/net/http/response.rs (L463-480)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<HttpResponsePreamble, CodecError> {
        // realistically, there won't be more than HTTP_PREAMBLE_MAX_NUM_HEADERS headers
        let mut headers = [httparse::EMPTY_HEADER; HTTP_PREAMBLE_MAX_NUM_HEADERS];
        let mut resp = httparse::Response::new(&mut headers);

        let buf_read = read_to_crlf2(fd)?;

        // consume response
        match resp.parse(&buf_read).map_err(|e| {
            CodecError::DeserializeError(format!("Failed to parse HTTP response: {:?}", &e))
        })? {
            httparse::Status::Partial => {
                // try again
                return Err(CodecError::UnderflowError(
                    "Not enough bytes to form a HTTP response preamble".to_string(),
                ));
            }
            httparse::Status::Complete(_) => {
```

**File:** stackslib/src/net/httpcore.rs (L1561-1584)
```rust
    /// Read the next HTTP preamble (be it a request or a response), and return the preamble and
    /// the number of bytes consumed while reading it.
    fn read_preamble(&mut self, buf: &[u8]) -> Result<(StacksHttpPreamble, usize), NetError> {
        // does this contain end-of-headers marker, including the last four bytes of preamble we
        // saw?
        if self.body_start.is_none() {
            for i in 0..=buf.len() {
                let window = self.body_start_search_window(i, buf);
                if window == [b'\r', b'\n', b'\r', b'\n'] {
                    self.body_start = Some(self.num_preamble_bytes + i);
                }
            }
        }
        if self.body_start.is_none() {
            // haven't found the body yet, so update `last_four_preamble_bytes`
            // and report underflow
            let len = buf.len();
            let last_four_preamble_bytes = self.body_start_search_window(len, buf);
            self.num_preamble_bytes += len;
            self.last_four_preamble_bytes = last_four_preamble_bytes;
            return Err(NetError::UnderflowError(
                "Not enough bytes to form HTTP preamble".into(),
            ));
        }
```

**File:** stackslib/src/net/http/request.rs (L302-319)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<HttpRequestPreamble, CodecError> {
        // realistically, there won't be more than HTTP_PREAMBLE_MAX_NUM_HEADERS headers
        let mut headers = [httparse::EMPTY_HEADER; HTTP_PREAMBLE_MAX_NUM_HEADERS];
        let mut req = httparse::Request::new(&mut headers);

        let buf_read = read_to_crlf2(fd)?;

        // consume request
        match req.parse(&buf_read).map_err(|e| {
            CodecError::DeserializeError(format!("Failed to parse HTTP request: {:?}", &e))
        })? {
            httparse::Status::Partial => {
                // partial
                return Err(CodecError::UnderflowError(
                    "Not enough bytes to form a HTTP request preamble".to_string(),
                ));
            }
            httparse::Status::Complete(_) => {
```
