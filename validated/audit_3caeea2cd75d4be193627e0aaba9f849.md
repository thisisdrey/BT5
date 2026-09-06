### Title
Unbounded HTTP response buffering in `libsigner`'s `run_http_request` allows a malicious/compromised Stacks node to exhaust signer memory - (File: libsigner/src/http.rs)

### Summary
`run_http_request` in `libsigner/src/http.rs` reads an entire HTTP response from the socket into an unbounded `Vec<u8>` via `sock.read_to_end(&mut buf)` before any length or content-length validation occurs. Unlike the node's own `StacksHttp` protocol implementation (`stackslib/src/net/httpcore.rs`, `stackslib/src/net/connection.rs`), which enforces `MAX_MESSAGE_LEN`/`HTTP_PREAMBLE_MAX_ENCODED_SIZE` bounds on both preamble and payload buffering, this signer-side HTTP client has no such ceiling. A malicious or compromised Stacks node that the signer talks to (e.g., because the signer's configured RPC endpoint is attacker-controlled, or a man-in-the-middle) can respond with an endless, never-terminating stream of bytes and force the signer process's heap to grow without bound, mirroring the `gun_http` CWE-770 pattern of "append bytes to a buffer with no upper-bound check while waiting for a terminator/EOF that never arrives."

### Finding Description
`run_http_request` is defined as: [1](#0-0) 

The critical lines are: [2](#0-1) 

`sock.read_to_end(&mut buf)` will keep growing `buf` for as long as the peer keeps sending bytes and does not close the connection — there is no maximum size check on `buf` before or during this read, and no timeout/size-limited reader is used. Only after the entire (potentially unbounded) response has been buffered does the code call `decode_http_response(&buf)` to parse headers and validate a `Content-Length`/status code.

This is the direct analog of the `gun_http:handle/5` bug: in `gun`, `gun_http`'s `head`, `body_chunked`, and `body_trailer` clauses accumulate bytes into `state.buffer` with binary concatenation and no upper bound, waiting for a terminator (`\r\n\r\n`, chunk boundary, or trailer) that a malicious server can simply never send. Here, `run_http_request` does the rust equivalent: it accumulates the full response with `read_to_end` before it has even parsed the headers or established a content length, so a peer that just keeps streaming bytes (or that keeps the connection open and dribbles data) causes the vector to grow indefinitely.

By contrast, the node's own HTTP protocol machinery in `stackslib/src/net` is careful to bound buffering:
- Preamble buffering is capped at `protocol.preamble_size_hint()`: [3](#0-2) 
- Payload buffering is capped at `MAX_MESSAGE_LEN` (or the known Content-Length): [4](#0-3) 
- Chunked-transfer decoding enforces a hard `max_size` ceiling on decoded byte count: [5](#0-4) 

`libsigner/src/http.rs::run_http_request` has none of these protections for the raw socket read; only the *chunked-body decode* path (`decode_http_body`) bounds itself with `MAX_MESSAGE_LEN` via `HttpChunkedTransferReader`, but that bound is applied only after the unbounded `read_to_end` has already completed (or never completes, in the attack scenario).

### Impact Explanation
A malicious or compromised Stacks node — which a signer must necessarily communicate with over HTTP to fetch chain state, submit block-proposal responses, etc. — can respond to any `run_http_request` call with an endless byte stream (or a very slow trickle of bytes on a kept-open connection) and drive the signer process's memory usage without bound, crashing the signer node (OOM). This is a remote, unauthenticated-by-the-signer resource-exhaustion vector against the signer process, matching the CWE-770 class in the report. It does not require the node's private key, the signer's key, or any privileged role — only that the signer initiate (or is tricked into initiating) an HTTP request against a party controlling the response stream.

### Likelihood Explanation
Any Stacks node that a signer is configured to communicate with is in a position to trigger this: a rogue/compromised node operator, or a MITM on the signer-to-node RPC link, can trivially withhold connection closure while sending garbage bytes. No cryptographic material or race condition is needed — it is a straightforward abuse of the missing size bound in `sock.read_to_end`.

### Recommendation
Bound the read in `run_http_request` (and any similar unbounded reads in `libsigner`) with an explicit maximum response size (e.g., `MAX_MESSAGE_LEN`), analogous to the bounding already applied to preamble/payload parsing in `stackslib/src/net/connection.rs` and to chunked-body decoding via `HttpChunkedTransferReaderState::max_size`. Practically, replace the unconditioned `sock.read_to_end(&mut buf)` with a bounded reader (e.g., `Read::take(MAX_MESSAGE_LEN)`) or incrementally parse headers first, honor `Content-Length`, and read at most that many bytes (plus a small header allowance), erroring out if the declared/observed size exceeds the cap.

### Proof of Concept
1. Configure a signer to point at an attacker-controlled "Stacks node" HTTP endpoint (or interpose as a MITM on the signer→node RPC connection).
2. Have the signer issue any RPC call that goes through `run_http_request` (`libsigner/src/http.rs:221`).
3. On the server side, respond with a valid-looking status line/headers (optionally omitting `Content-Length`, or using `Transfer-Encoding: chunked` with an endless stream of `X`-byte chunk boundaries that never terminate with the `0\r\n\r\n` marker), and simply never close the TCP connection while continuing to send arbitrary bytes.
4. Observe `buf` inside `run_http_request` (`libsigner/src/http.rs:249-251`) grow unboundedly as `sock.read_to_end(&mut buf)` keeps reading, since no cap exists on `buf`'s size before parsing — eventually exhausting the signer process's memory.

### Citations

**File:** libsigner/src/http.rs (L221-262)
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
}
```

**File:** stackslib/src/net/connection.rs (L679-699)
```rust
    fn buffer_preamble_bytes(&mut self, protocol: &mut P, bytes: &[u8]) -> usize {
        let max_preamble_len = protocol.preamble_size_hint();
        let Some(preamble_remaining) = max_preamble_len.checked_sub(self.buf.len()) else {
            return 0;
        };

        let to_consume = bytes.len().min(preamble_remaining);

        let _len = self.buf.len();
        self.buf.extend_from_slice(
            bytes
                .get(..to_consume)
                .expect("FATAL: bad buffer length check"),
        );

        trace!(
            "Buffer {to_consume} bytes out of max {max_preamble_len} for preamble (buf went from {_len} to {} bytes)",
            self.buf.len()
        );
        to_consume
    }
```

**File:** stackslib/src/net/connection.rs (L764-795)
```rust
    /// buffer up bytes for a message
    #[cfg_attr(test, mutants::skip)]
    fn buffer_message_bytes(
        &mut self,
        bytes: &[u8],
        message_len_opt: Option<usize>,
    ) -> Result<usize, net_error> {
        let message_len = message_len_opt.unwrap_or(MAX_MESSAGE_LEN as usize);
        let buffered_so_far = self
            .buf
            .len()
            .checked_sub(self.message_ptr)
            .ok_or_else(|| {
                net_error::RecvError(format!("Message ptr {} overran buffer", self.message_ptr))
            })?;

        let Some(message_remaining) = message_len.checked_sub(buffered_so_far) else {
            // can happen if we receive so much data when parsing the preamble that we've
            // also already received the message, and part of the next preamble (or more).
            return Ok(0);
        };

        let to_consume = bytes.len().min(message_remaining);

        trace!("Consume {} bytes from input buffer", to_consume);
        self.buf.extend_from_slice(
            bytes
                .get(..to_consume)
                .expect("FATAL: bad length check in buffer handling"),
        );
        Ok(to_consume)
    }
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
