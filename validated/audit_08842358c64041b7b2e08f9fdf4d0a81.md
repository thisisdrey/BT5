### Title
`get_latest_chunks` allocates the full attacker-controlled HTTP response body before enforcing `STACKERDB_MAX_CHUNK_SIZE`/`SIGNERS_STACKERDB_CHUNK_SIZE` - ([File: libsigner/src/session.rs])

### Summary
`StackerDBSession::get_latest_chunks` only checks `body_bytes.len() > limit` after `rpc_request` has already returned the fully-read response body. The underlying transport, `run_http_request` in `libsigner/src/http.rs`, reads the entire socket response via `sock.read_to_end(&mut buf)` with no size cap, and for non-chunked bodies `decode_http_body` just does `buf.to_vec()` with no bound at all. A node that is queried by a signer/miner's `StackerDBSession` (the "host" side of this client request) can return an arbitrarily large HTTP body, forcing the querying process to allocate memory proportional to the attacker-chosen size before the size check ever runs.

### Finding Description
The claimed equality/fault is: "bytes allocated for `body_bytes`" == "the `limit` the code claims to enforce," but in reality the allocation is driven entirely by the wire data, not by `limit`.

Tracing the path:
- `StackerDBSession::rpc_request` (`libsigner/src/session.rs:162-172`) calls `run_http_request(sock, ...)`.
- `run_http_request` (`libsigner/src/http.rs:221-262`) does `sock.read_to_end(&mut buf)` (line 251) with **no size limit whatsoever** — this reads until the socket is closed/EOF, allocating as much memory as the peer sends.
- The response is then split into headers/body via `decode_http_response`, and the body is extracted via `decode_http_body` (`libsigner/src/http.rs:197-217`). For the **chunked** transfer-encoding case it does bound decoding via `HttpChunkedTransferReader` using `MAX_MESSAGE_LEN`, but for the **non-chunked** case (line 211-213) it simply does `buf.to_vec()` — again unbounded, bounded only by whatever `read_to_end` already allocated.
- Only after this full body is materialized in memory does `get_latest_chunks` (`libsigner/src/session.rs:230-262`) check `if body_bytes.len() > limit { None } else { Some(body_bytes) }` (lines 243-247). By this point the oversized allocation has already happened; the check merely discards the reference, it does not prevent or bound the allocation.

So the equality the question describes is false: the enforcement of `limit` (`STACKERDB_MAX_CHUNK_SIZE` or `SIGNERS_STACKERDB_CHUNK_SIZE`) happens strictly after the full, attacker-sized buffer has been read into memory (`read_to_end`) and copied (`buf.to_vec()`), not before or during the read. No length header, no bounded `Read::take`, and no early-abort mechanism exists on this path to cap the number of bytes pulled off the socket for a non-chunked response.

### Impact Explanation
Any StackerDB replica host that a node's `StackerDBSession` queries (via `get_latest_chunks`/`get_latest_chunk`, used by signers and miners to read stackerdb chunks such as block proposals/vote messages) can, upon receiving a `GET` request for a chunk, respond with an HTTP response whose body is many times larger than the nominal chunk-size limit (limited only by the sender's willingness to send bytes and the receiving process's available memory, since `read_to_end` has no cap). This forces the querying node/signer process to allocate memory equal to the attacker's chosen body size on every such call, which is a memory-exhaustion primitive against the calling node/signer process. This is repeatable per request and requires no privileged role — the attacker only needs to be the responding party at `self.host` for a StackerDB query. This matches the "bounded compute/allocation DoS on a read path" characterization, though it should be noted the same `read_to_end`-without-limit pattern affects *all* uses of `run_http_request` (i.e., `list_chunks`, `get_chunks`, `put_chunk`'s ack read), not just `get_latest_chunks` — the size check specific to `get_latest_chunks` is simply the most visible instance of a systemic missing pre-allocation bound in `run_http_request`/`decode_http_body`.

### Likelihood Explanation
The attacker must be the responder for a `StackerDBSession` HTTP GET request — i.e., control (or MITM) the `host` that a signer/miner is configured to query for StackerDB chunks. This is realistic in deployments where nodes fetch StackerDB replicas from other, less-trusted, network peers, and requires no possession of any secret, admin role, or signature forgery — only the ability to accept a TCP connection and write bytes back, which matches the allowed "run their own peer" capability. Repeating the attack costs the attacker nothing beyond bandwidth to produce the oversized response (note: while unbounded read is real, the actual achievable severity is capped by attacker's own upload bandwidth and the victim's socket read timeout, which somewhat limits blast radius versus a "few messages, unbounded" crash).

### Recommendation
Bound the number of bytes read in `run_http_request`/`decode_http_body` before allocation, e.g., use a length-limited reader (`Read::take(MAX_ALLOWED)` sized to the maximum legitimate signer chunk size plus header overhead) instead of unconditional `read_to_end`, and reject/abort the connection as soon as the running total exceeds the applicable `limit`, rather than reading everything and checking `body_bytes.len()` afterward. Apply the same bound to the chunked-decoding path's `MAX_MESSAGE_LEN` usage as well, ensuring it is tightened to the StackerDB chunk-size limits rather than the larger generic message limit.

### Proof of Concept
```rust
// libsigner/src/session.rs (test module) or a new integration test
use std::io::Write;
use std::net::TcpListener;
use std::thread;
use std::time::Duration;
use clarity::vm::types::QualifiedContractIdentifier;
use libsigner::{SignerSession, StackerDBSession};

#[test]
fn get_latest_chunks_allocates_before_limit_check() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();

    // Oversized body: e.g. 10x STACKERDB_MAX_CHUNK_SIZE
    let oversized_len = 10 * libstackerdb::STACKERDB_MAX_CHUNK_SIZE as usize;

    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let body = vec![b'A'; oversized_len];
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(headers.as_bytes()).unwrap();
            stream.write_all(&body).unwrap();
        }
    });

    let contract_id = QualifiedContractIdentifier::transient(); // non-"signer" prefixed
    let mut session = StackerDBSession::new(
        &addr.to_string(),
        contract_id,
        Duration::from_secs(5),
    );

    // Track process memory / instrument run_http_request to assert peak allocation
    // equals oversized_len (attacker-controlled), NOT bounded by STACKERDB_MAX_CHUNK_SIZE.
    let result = session.get_latest_chunks(&[0]).unwrap();

    // Confirms the limit check happens post-hoc: result[0] is None (rejected),
    // but the full oversized_len buffer was already allocated and copied
    // inside run_http_request's `sock.read_to_end(&mut buf)` and
    // `decode_http_body`'s `buf.to_vec()` before this point.
    assert_eq!(result, vec![None]);
    // Instrumented allocation counter (e.g. via a custom global allocator) would show
    // peak_bytes >= oversized_len, violating the intended STACKERDB_MAX_CHUNK_SIZE bound.
}
```
The crash/DoS site to instrument is `libsigner/src/http.rs:251` (`sock.read_to_end(&mut buf)`) and `libsigner/src/http.rs:213` (`buf.to_vec()`); the size check that arrives too late is `libsigner/src/session.rs:243`.