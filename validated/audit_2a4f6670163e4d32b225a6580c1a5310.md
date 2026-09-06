### Title
Unbounded body read in `process_event` allows remote memory-exhaustion DoS via oversized `Content-Length` - (File: libsigner/src/events.rs)

### Summary
`process_event::<T, E>` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` with no upper bound check on size before attempting deserialization. Any remote party able to reach the signer's bound event-receiver TCP port can send a POST with a `Content-Length` header declaring an arbitrarily large body, causing the signer process to attempt an unbounded allocation.

### Finding Description
The claimed fault—no validated maximum length enforced before `read_to_string`—is confirmed directly in the code. `process_event` at `libsigner/src/events.rs:519-542` does:
```
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
```
There is no check of `request.body_length()` (which `tiny_http::Request` exposes from the `Content-Length` header) against `MAX_MESSAGE_LEN`, `BLOCK_RESPONSE_DATA_MAX_SIZE`, or any StackerDB chunk cap prior to reading. The size cap for StackerDB chunks (`BLOCK_RESPONSE_DATA_MAX_SIZE`, imported at line 47) is only relevant to payload semantics after full deserialization inside downstream `TryFrom` conversions (e.g., `signer_event::TryFrom<StackerDBChunksEvent>` at line 544+); it is never consulted to bound how many bytes `read_to_string` will accept.

`read_to_string` will grow the destination `String`'s buffer to match whatever `Read::read` returns, and `tiny_http`'s reader for a request with an explicit `Content-Length` will attempt to read up to that declared length (it does not itself cap allocation size). This means the attacker fully controls how large the buffer the signer process allocates becomes, by simply declaring a large `Content-Length` and sending (or slow-drip sending) that many bytes—no signature, secret, or authentication check occurs before this allocation, since the signer's HTTP event-receiver endpoint is an unauthenticated local listener meant for the node's event-dispatcher, but nothing in `process_event` verifies the caller's identity before consuming the body.

The event-receiver's bound port (`SignerEventReceiver::bind`) is set up to listen for HTTP POSTs from the configured event observer, but the underlying `tiny_http::Server` accepts connections from any TCP peer that can reach the configured bind address; there is no IP allow-list or authentication token check in `process_event` before the body is read into memory. Existing guards like `MAX_MESSAGE_LEN`/`BLOCK_RESPONSE_DATA_MAX_SIZE` are applied only to parsed message contents, not to the raw amount of memory allocated to receive the HTTP body.

### Impact Explanation
A single crafted POST to the signer's event-receiver endpoint (e.g., `/stackerdb_chunks`) with a large declared `Content-Length` forces the signer process to allocate a correspondingly large `String` buffer before any validation occurs, matching a remote unauthenticated single-message DoS/memory-exhaustion category. If the declared length is large enough (e.g., many gigabytes) relative to available system memory, the allocation can exhaust memory and crash or severely degrade the signer process, which is a critical component for network security (block signing). This is repeatable per connection/request.

### Likelihood Explanation
The event-receiver's TCP port must be reachable by the attacker. In default/production deployments this endpoint is typically bound to localhost and fed only by the local `stacks-node` event dispatcher, which would make this endpoint not remotely reachable by an "unprivileged remote party" as defined in the rules unless the operator has configured it to bind to a non-loopback interface. The question's scenario explicitly assumes "a remote party who can reach the signer event-receiver's bound TCP port," which is a precondition outside of what's guaranteed by default configuration; I could not verify from the available code whether the bind address is hard-restricted to loopback or configurable to a public interface. This affects the confidence in real-world remote reachability, but the code-level allocation-bound issue inside `process_event` itself is real and verifiable independent of deployment binding.

### Recommendation
Before calling `read_to_string`, check `request.body_length()` (if available via `tiny_http`) against a fixed maximum (e.g., `MAX_PAYLOAD_LEN` or a new constant sized to the largest legitimate StackerDB/event payload) and reject/abort requests exceeding it without allocating a buffer of that size; alternatively, use a bounded/limited reader (e.g., `Read::take(max_len)`) so that even a lying `Content-Length` cannot cause allocation beyond the cap, and enforce the same cap incrementally while streaming rather than relying on a single pre-check.

### Proof of Concept
A Rust test in `libsigner` would: 1) call `SignerEventReceiver::bind` to obtain a listening address; 2) open a raw `TcpStream` to it; 3) write a raw HTTP/1.1 request line `POST /stackerdb_chunks HTTP/1.1\r\n`, headers including `Content-Length: 4294967296` (4GB) and `Content-Type: application/json`, followed by `\r\n\r\n`; 4) send only a small number of body bytes and then stall/never complete the rest; 5) observe/assert (via a memory-monitoring hook or by using a moderately large but completable body, e.g. 500MB, in a resource-constrained test harness) that `process_event` calls `read_to_string` and attempts to grow the `body` `String` to the full declared size before any cap check, i.e., assert there is no early return/rejection based on `Content-Length` prior to the `read_to_string` call at `libsigner/src/events.rs:526`. [1](#0-0)

### Citations

**File:** libsigner/src/events.rs (L519-542)
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

    let signer_event: SignerEvent<T> = json_event.try_into()?;

    Ok(signer_event)
}
```
