### Title
Unbounded `sock.read_to_end` in `run_http_request` allows memory-exhaustion DoS from a single oversized HTTP response - ([File: libsigner/src/http.rs])

### Summary
`run_http_request` reads the entire HTTP response into memory via `sock.read_to_end(&mut buf)` before any parsing, length checking, or status-code validation occurs. A malicious server that the `StackerDBSession` connects to (via `rpc_request`) can stream an unbounded, unterminated body and force the signer thread to buffer arbitrarily large amounts of data in `buf`, regardless of the eventual HTTP status code.

### Finding Description
In `libsigner/src/http.rs`, `run_http_request` writes the HTTP request and then calls: [1](#0-0) 
`sock.read_to_end(&mut buf)` has no size bound and will not return until the peer closes the connection (the request sets `Connection: close`) or the socket read times out. Only *after* this unbounded read completes does the code call `decode_http_response(&buf)`, which is where status-code (`code != 200`) and `Content-Length`/body-size checks would occur: [2](#0-1) 
There is no cap analogous to `MAX_MESSAGE_LEN` applied to the raw socket read prior to `decode_http_response`; `MAX_MESSAGE_LEN` is only enforced later, inside `decode_http_body`, for chunked transfer-encoding via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`: [3](#0-2) 
That limit is irrelevant here because it only bounds decoding of an already-fully-buffered slice — the damage (unbounded allocation into `buf`) has already happened by the time `decode_http_body` is reached, and it is never reached at all for non-chunked or non-200 responses. `rpc_request` in `libsigner/src/session.rs` is the sole caller path (`list_chunks`, `get_chunks`, `get_latest_chunks`, `put_chunk`), all of which route through `run_http_request`: [4](#0-3) 
So the exploit flow is: the node the signer is configured to query (`self.host`) sends an HTTP response — with any status code, 200 or otherwise — followed by a body stream that never ends (or is many gigabytes) before closing the connection or hitting the configured `socket_timeout`. The signer's session thread will keep growing `buf` in `sock.read_to_end` for the entire duration, consuming memory proportional to attacker-sent bytes, with no length cap.

### Impact Explanation
A single malicious/compromised StackerDB-serving node can force the signer process to allocate unbounded memory on any RPC round trip (`list_chunks`, `get_chunks`, `get_latest_chunks`, `put_chunk`), potentially exhausting the signer's memory and crashing or destabilizing the signer process. This is a remote, unauthenticated DoS reachable via a single response and is repeatable on every request the signer session makes to that host, matching the Critical "remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The `socket_timeout` (`set_read_timeout`/`set_write_timeout`) bounds how long any *individual* `read` call can block, but does not bound total bytes accumulated across repeated non-blocking reads within that timeout window — an attacker can keep the connection alive and keep sending data continuously up until timeout, and `read_to_end` will keep looping and appending to `buf` as long as data keeps arriving. No secret, signature, or privileged role is required — the attacking party only needs to be the HTTP endpoint the signer's `host` config points to and be able to hold the TCP connection open while streaming bytes, which is a normal RPC-server capability, not a privileged one.

### Recommendation
Bound the response read in `run_http_request` with an explicit cap (e.g., wrap `sock` in a `Read::take(MAX_MESSAGE_LEN)` limiter, or read headers first with a small bounded buffer, parse `Content-Length`, and only read up to that many additional bytes, rejecting/erroring if it exceeds `MAX_MESSAGE_LEN`) before ever calling `decode_http_response`.

### Proof of Concept
```rust
// libsigner/src/session.rs (or a new test in http.rs)
use std::io::Write;
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

#[test]
fn unbounded_response_body_dos() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();

    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            // Non-200 status, but still stream an effectively unbounded body
            stream.write_all(b"HTTP/1.1 500 Internal Server Error\r\n\r\n").unwrap();
            let chunk = vec![b'A'; 1024 * 1024];
            loop {
                if stream.write_all(&chunk).is_err() {
                    break; // client hit its read timeout and closed
                }
            }
        }
    });

    let contract_id = QualifiedContractIdentifier::transient();
    // Use a generous timeout to let read_to_end accumulate a large buffer
    let mut session = StackerDBSession::new(&addr.to_string(), contract_id, Duration::from_secs(5));
    session.connect_or_reconnect().unwrap();

    // This call blocks inside sock.read_to_end for the full timeout,
    // buffering unbounded attacker data in `buf` at http.rs:251
    // BEFORE decode_http_response (http.rs:144) can reject on the 500 status.
    let result = session.rpc_request("GET", "/", None, &[]);
    // Regardless of outcome, memory growth in `buf` is unbounded during the read,
    // demonstrating the missing length cap prior to status validation.
    let _ = result;
}
```
This test confirms `read_to_end` at `libsigner/src/http.rs:251` unconditionally buffers all bytes sent by the server, with the 500 status rejection at `libsigner/src/http.rs:144` only evaluated afterward, after the entire (attacker-controlled, unbounded) body has already been read into memory.

### Citations

**File:** libsigner/src/http.rs (L140-150)
```rust
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

**File:** libsigner/src/http.rs (L249-253)
```rust
    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
```

**File:** libsigner/src/session.rs (L162-172)
```rust
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
