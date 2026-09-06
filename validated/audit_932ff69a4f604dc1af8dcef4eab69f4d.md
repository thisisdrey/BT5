### Title
Unbounded `read_to_string` in `process_event` allows single-message memory-exhaustion DoS via `/proposal_response` - (File: libsigner/src/events.rs)

### Summary
The `/proposal_response` route dispatches to the same generic `process_event::<T, BlockValidateResponse>` function used by `/stackerdb_chunks`, which reads the entire HTTP request body into memory with `request.as_reader().read_to_string(&mut body)` and no size cap. Any TCP peer that can reach the signer's event-receiver HTTP server can POST an arbitrarily large body to this endpoint and cause unbounded memory allocation before any parsing or validation occurs.

### Finding Description
`SignerEventReceiver::next_event` routes POST requests based on URL, mapping `/proposal_response` to `process_event::<T, BlockValidateResponse>` exactly as `/stackerdb_chunks` maps to `process_event::<T, StackerDBChunksEvent>` [1](#0-0) . Inside `process_event`, the body is read with no length limit: `request.as_reader().read_to_string(&mut body)` — there is no check of `Content-Length`, no `take()`/`limit()` wrapper, and no `MAX_PAYLOAD_LEN`-style cap applied before or during the read [2](#0-1) . Only after the full body has been buffered in memory does `serde_json::from_slice::<E>` attempt to parse it [3](#0-2) . Since `E` is instantiated as `BlockValidateResponse` for this route, the type parameter differs but the vulnerable code path — the unbounded read — is identical and shared between both routes, with no per-route or shared byte cap distinguishing them.

An attacker who can open a TCP connection to the signer's event-receiver listening address can send `POST /proposal_response HTTP/1.1` with a very large `Content-Length` and body (e.g., gigabytes), forcing the process to allocate memory proportional to the attacker-supplied body size in a single request, before any authentication, signature check, or JSON validation happens.

### Impact Explanation
A single oversized POST to `/proposal_response` causes the signer's event-receiver thread to allocate memory unbounded by any cap, which can exhaust available memory and crash or stall the `stacks-signer` process — a Critical, remote, unauthenticated, single-message DoS. This affects the signer node's event ingestion path, potentially causing the signer to miss block validation responses and impacting availability.

### Likelihood Explanation
Preconditions are minimal: only TCP reachability to the signer's event-receiver HTTP listener is needed, no secret, no peer identity, no privileged role. The attack requires a single crafted POST request and is trivially repeatable at will.

### Recommendation
Enforce a maximum request body size (e.g., check `Content-Length` against a `MAX_PAYLOAD_LEN`-style constant and reject/close early, or wrap the reader with `Read::take(MAX_PAYLOAD_LEN)`) before calling `read_to_string` in `process_event`, applied uniformly to all routes (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`).

### Proof of Concept
```rust
// libsigner/src/events.rs (test) — pseudo-outline
#[test]
fn oversized_body_to_proposal_response_causes_unbounded_alloc() {
    // 1. Bind a SignerEventReceiver<T> on 127.0.0.1:0
    // 2. Connect a TcpStream to the bound address
    // 3. Send:
    //    POST /proposal_response HTTP/1.1\r\n
    //    Content-Length: <huge, e.g. 2_000_000_000>\r\n\r\n
    //    <huge body of arbitrary bytes>
    // 4. Observe process_event's `request.as_reader().read_to_string(&mut body)`
    //    buffers the entire body with no cap, causing large/unbounded allocation
    //    prior to any serde_json::from_slice::<BlockValidateResponse> validation.
    // 5. Assert: process memory grows proportional to attacker-controlled body size,
    //    with no rejection based on size prior to full read completion.
}
``` [1](#0-0) [4](#0-3)

### Citations

**File:** libsigner/src/events.rs (L437-442)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
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
