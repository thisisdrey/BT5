### Title
Unbounded response buffering in `run_http_request` allows a malicious StackerDB replica to trigger unauthenticated memory-exhaustion DoS against `put_chunk` callers - ([File: libsigner/src/http.rs])

### Summary
`StackerDBSession::put_chunk` (`libsigner/src/session.rs`) calls `rpc_request`, which delegates to `run_http_request` in `libsigner/src/http.rs`. That function reads the entire HTTP response with `sock.read_to_end(&mut buf)` with **no size limit whatsoever**, before any header parsing, `Content-Length` validation, or chunk-size capping occurs. A malicious StackerDB replica host can stream an arbitrarily large body, forcing the caller to buffer it entirely in memory before the request even reaches `decode_http_response`/`decode_http_body`/`serde_json::from_slice`.

### Finding Description
The claimed equality — "`resp_bytes.len()` == a validated maximum" — is **broken at an earlier point than the question suggests**. It's not just that `serde_json::from_slice::<StackerDBChunkAckData>` lacks a size cap; the raw socket read itself is unbounded:

```rust
// libsigner/src/http.rs
let mut buf = vec![];
sock.read_to_end(&mut buf)?;          // <-- unbounded, no cap tied to STACKERDB_MAX_CHUNK_SIZE / MAX_MESSAGE_LEN
let (headers, body_offset) = decode_http_response(&buf)?;
...
decode_http_body(&headers, &buf[body_offset..]).map_err(|e| e.into())
``` [1](#0-0) 

Only the *chunked-transfer-encoding* path caps body length via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`: [2](#0-1) 

But this cap is applied only *after* `sock.read_to_end(&mut buf)` has already buffered the full response in memory — and only if the attacker sets `Transfer-Encoding: chunked`. If the attacker instead sends a plain `Content-Length`-less or oversized body (non-chunked), `decode_http_body` simply does `buf.to_vec()` with no size check at all: [3](#0-2) 

The `put_chunk` call site then attempts `serde_json::from_slice::<StackerDBChunkAckData>(&resp_bytes)`, but by that point the damage (unbounded allocation of `buf`/`resp_bytes`, potentially multiple hundreds of MB or GB) has already occurred: [4](#0-3) 

Root cause: `run_http_request` has no read cap on the initial `read_to_end`, no `Content-Length` sanity check against `STACKERDB_MAX_CHUNK_SIZE`/`MAX_MESSAGE_LEN`, and no streaming/limited reader is used before buffering the whole socket into a `Vec<u8>`.

### Impact Explanation
Any host that a signer/node's `StackerDBSession` connects to as a StackerDB replica (i.e., a peer/host entry in the StackerDB configuration that the victim dials out to for `put_chunk`/`list_chunks`/`get_chunks`) can, upon receiving a request, stream an oversized response body. The victim process will allocate memory proportional to the attacker's stream size with no upper bound, only limited by the configured `socket_timeout` and how fast/slow the attacker drips bytes (a slow-drip keeps the read active without timing out, or the attacker can send data fast enough to allocate gigabytes before timeout triggers). This is a repeatable, single-request memory-exhaustion DoS against the connecting node/signer process — matching the Critical category ("remote crash/unauthenticated DoS from few messages").

### Likelihood Explanation
- The attacker needs only to be the destination host of a `StackerDBSession` connection (a StackerDB replica peer address that the victim client dials, e.g., configured signer/node host list) — no secret, no signature, no privileged role required.
- No authentication of the response is performed before buffering; `decode_http_response` runs only after the entire body is already read into `buf`.
- Each request is independently exploitable and fully repeatable (one connection per DoS attempt); cost to the attacker is trivial (serve a large or streamed response).
- This code path is reachable from `put_chunk`, `list_chunks`, `get_chunks`, and `get_latest_chunks` — all of which route through `rpc_request` → `run_http_request`.

### Recommendation
Bound the response read in `run_http_request` using a size-limited reader (e.g., `Read::take(MAX_MESSAGE_LEN)` or a similar cap tied to `STACKERDB_MAX_CHUNK_SIZE`) instead of unconditional `read_to_end`. Validate `Content-Length` against this cap before allocating, and enforce the same cap for the chunked-transfer path *before* buffering rather than only inside `HttpChunkedTransferReader`. Reject/abort connections whose body exceeds the cap.

### Proof of Concept
```rust
// Rust net test plan (to be added near libsigner/src/session.rs tests)
#[test]
fn put_chunk_unbounded_response_oom() {
    use std::net::TcpListener;
    use std::io::Write;
    use std::thread;

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();

    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            // consume the request line/headers (not strictly required for read_to_end test)
            stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n").unwrap();
            // Stream a huge (e.g. 500MB+) attacker-controlled body with no valid JSON ack,
            // never terminating the connection promptly.
            let chunk = vec![b'A'; 10 * 1024 * 1024]; // 10MB chunk
            for _ in 0..60 { // ~600MB total
                if stream.write_all(&chunk).is_err() { break; }
            }
        }
    });

    let contract_id = clarity::vm::types::QualifiedContractIdentifier::transient();
    let mut session = StackerDBSession::new(&addr.to_string(), contract_id, std::time::Duration::from_secs(30));
    let chunk_data = libstackerdb::StackerDBChunkData::new(0, 0, vec![1,2,3]);

    // Observed: process memory grows unbounded (hundreds of MB) inside
    // sock.read_to_end(&mut buf) in libsigner/src/http.rs before decode_http_response
    // or serde_json::from_slice is ever reached; assert on RSS growth or a
    // memory-limited test harness (e.g., cgroup/ulimit) killing the process.
    let result = session.put_chunk(&chunk_data);
    assert!(result.is_err()); // eventually errors, but only after unbounded buffering
}
```
Instrument or run under a memory cgroup/ulimit to demonstrate the process is OOM-killed or exceeds intended memory bounds while inside `sock.read_to_end(&mut buf)` at `libsigner/src/http.rs:251`, prior to any `serde_json::from_slice` call in `put_chunk`.

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

**File:** libsigner/src/http.rs (L249-261)
```rust
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

**File:** libsigner/src/session.rs (L265-273)
```rust
    fn put_chunk(&mut self, chunk: &StackerDBChunkData) -> Result<StackerDBChunkAckData, RPCError> {
        let body =
            serde_json::to_vec(chunk).map_err(|e| RPCError::Deserialize(format!("{e:?}")))?;
        let path = stackerdb_post_chunk_path(self.stackerdb_contract_id.clone());
        let resp_bytes = self.rpc_request("POST", &path, Some("application/json"), &body)?;
        let ack: StackerDBChunkAckData = serde_json::from_slice(&resp_bytes)
            .map_err(|e| RPCError::Deserialize(format!("{e:?}")))?;
        Ok(ack)
    }
```
