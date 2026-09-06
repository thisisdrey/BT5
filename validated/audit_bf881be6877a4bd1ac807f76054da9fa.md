### Title
Unbounded `read_to_string` on signer event socket allows single-request memory exhaustion DoS - (File: libsigner/src/events.rs)

### Summary
`process_event` in `libsigner/src/events.rs` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` before any length check, deserialization, or authentication occurs. No cap analogous to `STACKERDB_MAX_CHUNK_SIZE` or `BLOCK_RESPONSE_DATA_MAX_SIZE` is applied at this ingestion point, so a single oversized POST can force the process to allocate an amount of memory proportional to the attacker-supplied body size.

### Finding Description
The invariant the codebase otherwise enforces elsewhere — "bytes buffered for one event/message are capped at a known ceiling before any parsing" — is broken at this specific ingestion point. `process_event` (libsigner/src/events.rs:519-542) does:

```rust
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
``` [1](#0-0) 

`request.as_reader()` returns a reader over the incoming TCP stream driven purely by whatever `Content-Length` (or chunked transfer bytes) the client claims; `read_to_string` will keep growing the `String` buffer until EOF or an I/O error, with no maximum-size guard applied before or during the read. Only after the entire body has been buffered does the code call `serde_json::from_slice` for deserialization (libsigner/src/events.rs:536) — by then the (potentially huge) allocation has already happened. This is dispatched from `next_event`/the request-routing match arms for endpoints such as `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` (libsigner/src/events.rs:440-457), all of which route through this same unguarded `process_event`.

### Impact Explanation
Any party able to open a TCP connection to the signer's event-receiver HTTP port and issue a POST with a very large body (with a correspondingly large or absent `Content-Length`, or using chunked encoding) forces the signer process to allocate memory proportional to that body before any validation or authentication takes place. Repeating this (or issuing several large requests, including in parallel) can exhaust the signer's memory and crash or severely degrade it — a single-message/few-message unauthenticated DoS against the signer's event ingestion path.

### Likelihood Explanation
Exploitability depends entirely on whether the signer's event HTTP listener is reachable by an unprivileged remote party. This endpoint is designed for the local `stacks-node` to POST observer events to the signer (typically bound to a local/loopback address by node configuration), which is the normal deployment model. I was unable to fully confirm within the available context whether the bind address in this codebase is hard-restricted to loopback/localhost in all configurations or whether it can be configured to listen on a non-loopback interface — the `HttpServer::http`/bind-address construction site was not found in the indexed portion of `events.rs`. If the listener is only ever bound to loopback and never remotely reachable, this finding does not meet the "unprivileged remote attacker" precondition required by the rules and would be out of scope. If it can be bound to a non-loopback address (a legitimate operator misconfiguration is out of scope per the rules, but a default binding to `0.0.0.0` would not be), the attack is trivially reachable with a single crafted POST and no credentials.

### Recommendation
Impose a maximum body-size ceiling at the top of `process_event` before invoking `read_to_string`, e.g. check `request.body_length()` against a `MAX_EVENT_BODY_LEN` constant (mirroring `STACKERDB_MAX_CHUNK_SIZE`/`BLOCK_RESPONSE_DATA_MAX_SIZE`) and reject/close the connection if it exceeds the limit, or use a bounded reader (`Read::take(MAX_EVENT_BODY_LEN)`) so allocation cannot exceed the intended ceiling regardless of what the client claims or streams.

### Proof of Concept
```rust
// libsigner/src/events.rs (test) or a standalone integration test
// 1. Construct a SignerEventReceiver bound to 127.0.0.1:<port> (as done in existing tests for this module).
// 2. Open a raw std::net::TcpStream to that port.
// 3. Write an HTTP POST request line for "/stackerdb_chunks" with a Content-Length header
//    set to e.g. 500_000_000, then stream 500MB of arbitrary bytes to the socket.
// 4. Observe process RSS grow to ~500MB inside process_event's `read_to_string` call
//    (libsigner/src/events.rs:526), well before serde_json::from_slice (line 536) is ever reached,
//    confirming the allocation is unconditional on payload validity.
// Assertion: process memory usage tracked via /proc/self/status (VmRSS) before and after the send
// shows growth proportional to the attacker-controlled body size, with no rejection or truncation.
```

### Citations

**File:** libsigner/src/events.rs (L524-533)
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
```
