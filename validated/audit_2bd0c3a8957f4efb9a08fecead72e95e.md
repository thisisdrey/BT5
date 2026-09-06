Confirmed: `process_event` at line 526 calls `request.as_reader().read_to_string(&mut body)` with no size cap, using the `tiny_http` crate's `Request` reader directly rather than going through `decode_http_body`/`MAX_MESSAGE_LEN` in `libsigner/src/http.rs`. [1](#0-0) 

This event-receiver HTTP server (`SignerEventReceiver::next_event`) is the endpoint that the stacks-node event dispatcher POSTs to on paths `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, dispatching each to `process_event::<T, E>`. [2](#0-1) 

### Title
Unbounded HTTP body buffering in signer event receiver enables single-request memory exhaustion - (File: libsigner/src/events.rs)

### Summary
`process_event` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` with no length cap, before any JSON parsing, signature check, or the `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN` guard that other code paths in `libsigner/src/http.rs` apply through `decode_http_body`. A single POST with a large `Content-Length` and matching body causes unbounded allocation on the signer's event-receiver thread.

### Finding Description
The broken invariant: elsewhere in this crate (`libsigner/src/http.rs::decode_http_body`), the HTTP body is decoded with an explicit bound via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())` for chunked bodies, and callers of `run_http_request` rely on this cap. However, the `tiny_http`-based server path used by `SignerEventReceiver::next_event` -> `process_event` bypasses this entirely: it calls `tiny_http::Request::as_reader()` and pipes it straight into `String`'s `read_to_string`, which loops until EOF, growing the destination buffer as needed — there is no check of `Content-Length` against any maximum before or during this read.

Attacker action: connect to the signer's event-receiver HTTP listener (bound locally to receive node-forwarded events, but reachable to anyone who can connect to that socket, e.g. if it's bound to a non-loopback interface or reachable via port forwarding/misconfiguration) and POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` with a very large `Content-Length` header and matching multi-gigabyte body. `process_event` will attempt to buffer the full body into memory before ever attempting `serde_json::from_slice` or any signature/state check in the `TryFrom` impls.

### Impact Explanation
A single crafted request can force multi-gigabyte allocation on the event-receiver thread, leading to OOM/crash of the signer process — a Critical, single-message remote DoS as defined by the audit's impact categories. This differs from a bandwidth-flood: a single connection with a `Content-Length`-matched body of attacker's choosing triggers unbounded buffering before any validation, satisfying the audit's carve-out against "volumetric DDoS...needing only traffic volume."

### Likelihood Explanation
No signer key, StackerDB slot ownership, or node RPC secret is needed — the event receiver has no authentication in this code path. The only precondition is network reachability to the signer's event-receiver port, which is a normal deployment target for stacks-node's event-observer POST callbacks; if this listener is bound to an interface reachable from outside the local node (a common operator misconfiguration this code doesn't prevent), an unprivileged remote attacker can trigger it repeatedly and cheaply (one TCP connection per attempt).

### Recommendation
Bound the body read in `process_event` before buffering, e.g. check the `Content-Length` header against a constant (such as `MAX_MESSAGE_LEN` or a new `MAX_EVENT_BODY_LEN`) and reject/close the connection if it exceeds the cap, or use a size-limited reader (`Read::take(MAX_LEN)`) when calling `read_to_string`, mirroring the existing bound already used in `decode_http_body`.

### Proof of Concept
```rust
// libsigner/src/tests/events.rs (new test)
use std::io::Write;
use std::net::TcpStream;

#[test]
fn test_large_body_oom_dos() {
    let mut receiver: SignerEventReceiver<SomeSignerMessage> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    std::thread::spawn(move || { let _ = receiver.next_event(); });

    let mut stream = TcpStream::connect(addr).unwrap();
    let body_len: usize = 2 * 1024 * 1024 * 1024; // 2 GiB
    let header = format!(
        "POST /stackerdb_chunks HTTP/1.1\r\nHost: {addr}\r\nContent-Length: {body_len}\r\nContent-Type: application/json\r\n\r\n"
    );
    stream.write_all(header.as_bytes()).unwrap();
    // stream a body_len-sized payload; observe process memory grow unbounded
    // in `process_event`'s `read_to_string` before any JSON parse occurs,
    // eventually causing allocation failure/OOM-kill of the process.
    let chunk = vec![b'a'; 1024 * 1024];
    for _ in 0..(body_len / chunk.len()) {
        stream.write_all(&chunk).unwrap();
    }
}
```
Assertion/crash site: `libsigner/src/events.rs:526` (`request.as_reader().read_to_string(&mut body)`), where allocation size is unbounded and proportional to the attacker-supplied `Content-Length`.

### Citations

**File:** libsigner/src/events.rs (L436-447)
```rust
            debug!("Processing {} event", request.url());
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

**File:** libsigner/src/events.rs (L519-537)
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
    // Regardless of whether we successfully deserialize, we should ack the dispatcher so they don't keep resending it
    ack_dispatcher(request);
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;
```
