### Title
Unbounded blocking body read in `process_event` hangs the signer's single-threaded event receiver - (File: `libsigner/src/events.rs`)

### Summary
`process_event` calls `request.as_reader().read_to_string(&mut body)` with no size cap and no read timeout on the underlying socket, and `SignerEventReceiver::next_event`/`main_loop` process requests one at a time on a single thread. A remote party who can reach the signer's event-listener TCP port can send a POST to `/stackerdb_chunks` (or any dispatched path) declaring a large `Content-Length` while sending only a partial body and never closing the connection, causing the read to block forever and starving all subsequent legitimate node-forwarded events.

### Finding Description
`SignerEventReceiver::next_event` dispatches based on `request.url()` to `process_event::<T, E>(request)` for `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` [1](#0-0) . `process_event` then performs an unbounded read of the whole body: `request.as_reader().read_to_string(&mut body)` [2](#0-1) . There is no maximum body length enforced before or during this read, and no `set_read_timeout` is applied anywhere on the `tiny_http::Server`/underlying `TcpStream` in this file (no `read_timeout`/`set_read_timeout` call exists in `libsigner/src/events.rs`). Because `tiny_http`'s reader for a request with `Content-Length: N` blocks until it has read `N` bytes (or the connection is closed/errors), a client that declares a large `Content-Length` and sends fewer bytes without closing the socket causes `read_to_string` to block indefinitely on that connection's socket read. Since `next_event` is invoked synchronously in `main_loop`'s single loop iteration, and `HttpServer::recv()`/`process_event` run on one thread with no concurrency, this one hung read blocks the entire receiver: no other request (including a legitimate node-forwarded `/stackerdb_chunks` POST carrying a `StackerDBChunksEvent`) can be accepted or processed until the malicious connection is closed or the process is killed. No authentication, secret, or signature check gates access to this HTTP endpoint before the body read occurs.

### Impact Explanation
A single crafted TCP connection to the signer's event-listener port causes an unbounded blocking read on the sole event-processing thread, indefinitely preventing the signer from receiving any subsequent legitimate `StackerDBChunksEvent` or `BlockValidateResponse` forwarded by the node. This is an unauthenticated single-message DoS against the signer's ingestion pipeline and is trivially repeatable (one connection per attack, can be re-triggered any time the receiver recovers).

### Likelihood Explanation
The only precondition is TCP reachability to the signer's bound event-listener address; no secret, peer identity, or slot ownership is required, and the attacker cost is a single crafted HTTP request line/header combination held open on one socket. No node/consensus state is required.

### Recommendation
Enforce a maximum request body size (reject/abort if `Content-Length` exceeds a bound, e.g. matching `MAX_PAYLOAD_LEN`-style caps used elsewhere) and use `request.as_reader().take(MAX_LEN)` for the read; additionally set a read/idle timeout on the accepted connection (e.g., via `tiny_http`'s underlying stream or a wrapping timeout) so a stalled sender cannot hold the processing thread hostage, and consider moving `next_event`'s per-request handling off the single accept loop (e.g., handle each connection/request read with a bounded timeout before dispatch).

### Proof of Concept
1. In a test, call `SignerEventReceiver::bind` on `127.0.0.1:0` to get the bound address, then spawn `next_event()` in a thread.
2. From a raw `std::net::TcpStream::connect` to that address, send:
   `POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: 100000000\r\n\r\n` followed by only a few bytes of body, then do not close or shutdown the write side.
3. Assert that `next_event()` (running in the spawned thread) does not return within a bounded timeout (e.g., `recv_timeout` on a channel signaling completion) — demonstrating the block occurs inside `request.as_reader().read_to_string(&mut body)` in `process_event` at `libsigner/src/events.rs:526`.
4. Confirm that a second, legitimate well-formed `/stackerdb_chunks` POST sent on a separate connection is never processed while the first connection remains open, proving the single-threaded receiver is starved.

### Citations

**File:** libsigner/src/events.rs (L437-447)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
                process_event::<T, StacksBlockEvent>(request)
```

**File:** libsigner/src/events.rs (L519-533)
```rust
fn process_event<T, E>(mut request: HttpRequest) -> Result<SignerEvent<T>, EventError>
where
    T: SignerEventTrait,
    E: serde::de::DeserializeOwned + TryInto<SignerEvent<T>, Error = EventError>,
{
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
```
