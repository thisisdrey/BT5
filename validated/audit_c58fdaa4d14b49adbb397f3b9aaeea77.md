### Title
Unbounded, timeout-free body read in `process_event` allows single-connection DoS of the signer event receiver - (File: libsigner/src/events.rs)

### Summary
`process_event` (called from `SignerEventReceiver::next_event` for `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) reads the entire HTTP body via `request.as_reader().read_to_string(&mut body)` with no maximum-size cap and no read timeout, before any JSON/codec/signature validation occurs. Because `next_event`/`main_loop` process one connection fully before accepting the next, a single unauthenticated client can send a POST with a huge `Content-Length` and either flood bytes to force a huge allocation or trickle bytes to block the thread indefinitely, freezing the signer's only event-processing loop.

### Finding Description
The claimed equality "bytes read for the body == a validated/bounded length" does not hold anywhere in this path. `process_event` at `libsigner/src/events.rs:519-542` does: [1](#0-0) 
There is no check of `Content-Length` against any `MAX_PAYLOAD_LEN`/`MAX_MESSAGE_LEN`-style cap, and no read timeout is set on the underlying stream before this call. `SignerEventReceiver::next_event` dispatches directly into this function for `/stackerdb_chunks` with zero authentication (no secret, no signature check on the raw bytes) prior to the read: [2](#0-1) 
`main_loop` calls `next_event()` synchronously and blocks on `http_server.recv()` / the subsequent body read before handling any further connections, since it is a single sequential loop: [3](#0-2) 
Any deserialization, signature checks (`chunk.recover_pk()`), or contract-id filtering in `TryFrom<StackerDBChunksEvent>` only happen after the full body has already been buffered into memory — they provide no protection against the read itself.

The attacker's message: open a TCP connection to the signer's bound event-receiver address and send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <addr>
Content-Length: 999999999999
Content-Type: application/json

<never send the body, or send it extremely slowly / with padding>
```
Because `read_to_string` blocks until it has consumed the declared `Content-Length` (or the connection dies), and no timeout is configured, this single connection can either (a) force a very large in-memory `String` allocation if the attacker actually sends the bytes, or (b) hang the sole processing thread indefinitely if the attacker trickles/withholds bytes (slow-POST). Either way, no legitimate signer traffic (proposals, burn blocks, stackerdb chunks) can be processed while the thread is stuck.

### Impact Explanation
This causes denial of service against the signer's event-receiver thread — the same thread responsible for delivering block proposals, burn-block events, and StackerDB chunk events to the signer runloop. A single malicious/misbehaving TCP client that can reach the bound address can freeze or OOM the process with one connection, and can repeat this indefinitely (or open several such connections) with negligible attacker cost. This matches "Critical — remote crash/unauthenticated DoS from few messages" since no signature, secret, or admin role is required to reach and exploit this code path.

### Likelihood Explanation
No authentication is required: the handler runs before any of the `TryFrom<StackerDBChunksEvent>` signature or contract-name checks. The only precondition is TCP reachability to the configured event-receiver bind address; this is an operator/deployment concern, but nothing in the code enforces localhost-only binding or authenticates callers at the transport layer — `bind` opens a plain `HttpServer::http(listener)` with no ACL. The attack requires a single crafted HTTP request and no special timing or state, and is trivially repeatable.

### Recommendation
- Enforce a maximum request-body size (mirroring `MAX_PAYLOAD_LEN`/`BLOCK_RESPONSE_DATA_MAX_SIZE`-style caps used elsewhere in the codebase) by validating the `Content-Length` header before reading, and by using a bounded reader (`Read::take(MAX_LEN)`) instead of unbounded `read_to_string`.
- Set an explicit read/write timeout on the accepted connection (tiny_http supports setting socket timeouts) so a stalled client cannot hang the processing thread.
- Consider moving body reading/parsing off the single serial loop (e.g., per-connection worker with bounded concurrency) so one slow/malicious peer cannot block all other event delivery.

### Proof of Concept
Rust test plan under `libsigner::events::tests` (net-based):
1. Construct a `SignerEventReceiver<T>`, call `bind()` on `127.0.0.1:0`, spawn `main_loop` (or directly call `next_event()`) in a thread, and register a consumer channel.
2. From the test (attacker) thread, open a `TcpStream` to the bound address and write:
   `"POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: 9999999999\r\nContent-Type: application/json\r\n\r\n"` then send only a few bytes of body, or none, and stall (`std::thread::sleep`) rather than closing the stream.
3. Assert that `next_event()` does not return within a bounded timeout (e.g., using a channel with `recv_timeout`), proving the thread is blocked inside `read_to_string` at `libsigner/src/events.rs:526` before any JSON parsing (`serde_json::from_slice` at line 536) or signature check (`chunk.recover_pk()` at line 596) is ever reached — demonstrating the DoS occurs strictly pre-validation.

### Citations

**File:** libsigner/src/events.rs (L284-312)
```rust
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
    }
```

**File:** libsigner/src/events.rs (L437-438)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
```

**File:** libsigner/src/events.rs (L524-537)
```rust
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
    // Regardless of whether we successfully deserialize, we should ack the dispatcher so they don't keep resending it
    ack_dispatcher(request);
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;
```
