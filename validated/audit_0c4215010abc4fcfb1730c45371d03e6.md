### Title
Unbounded body read in `process_event` allows single-request memory-exhaustion DoS against the signer's event listener - ([File: libsigner/src/events.rs])

### Summary
`process_event<T, E>` in `libsigner/src/events.rs` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` before any size validation or `serde_json` parsing occurs. There is no `Content-Length` cap, `MAX_MESSAGE_LEN`, or streaming/size-limited reader used anywhere in this path, so a single POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` with an arbitrarily large body forces the signer process to allocate memory proportional to the attacker-supplied body size.

### Finding Description
The fault is a missing bound: `read_to_string` on `tiny_http`'s request reader has no maximum-length guard, and `next_event` dispatches directly to `process_event` for all four POST endpoints without pre-checking `Content-Length` or truncating the stream: [1](#0-0) 

The dispatch sites that route unauthenticated remote input into this function are: [2](#0-1) 

No signature check, secret, or size cap exists between accepting the TCP connection (`http_server.recv()`) and the `read_to_string` call. `ack_dispatcher` runs only after the (already unbounded) read completes, so it cannot mitigate the allocation. Unlike other codec-based deserialization paths in the repo (e.g., `read_next_at_most(fd, BLOCK_RESPONSE_DATA_MAX_SIZE)` used for `BlockProposalData`), the JSON event path has no equivalent cap.

### Impact Explanation
A single crafted request with a large `Content-Length`/body causes the `stacks-signer` process to allocate memory equal to the body size before any validation, which can exhaust available memory or induce severe latency/allocator pressure on the signer's event-ingestion thread — this is a bounded compute/memory DoS on a read endpoint (matching the "High" impact category). It is repeatable per-connection and does not require the node or any peer secret; it only requires the ability to open a TCP connection to the port `SignerEventReceiver::bind` listens on.

### Likelihood Explanation
No authentication, signature, or node cooperation is required — the attacker only needs TCP reachability to the signer's bound event-listener address and to send a well-formed HTTP POST to one of the four routed paths with an oversized body. This makes the attack low-cost and repeatable. The main open question is deployment-specific: if operators only bind this listener to loopback or a firewalled internal interface (as commonly recommended for signer event ports), external reachability is reduced, but nothing in the code itself enforces or requires that restriction — the vulnerable code path is reachable by design from any host that can connect to the configured address.

### Recommendation
Add an explicit maximum body-size check before reading: validate the `Content-Length` header against a `MAX_MESSAGE_LEN`-style constant and reject/close requests that exceed it, and/or use a bounded reader (e.g., `Read::take(MAX_LEN)`) when calling `read_to_string`/`read_to_end` in `process_event`, mirroring the `read_next_at_most` pattern already used for `BlockProposalData`.

### Proof of Concept
1. Start a `SignerEventReceiver<T>` via `bind()` on `127.0.0.1:0` as done in existing tests in this module.
2. From a separate thread/process, open a `TcpStream` to the bound address and send:
   `POST /new_block HTTP/1.1\r\nHost: <addr>\r\nContent-Length: <N>\r\nContent-Type: application/json\r\n\r\n` followed by `N` bytes (e.g., `N` = several hundred MB of filler bytes, not valid JSON).
3. Call `next_event()` on the receiver and observe that the process's resident memory grows by ~`N` bytes during `request.as_reader().read_to_string(&mut body)` in `process_event`, well before `serde_json::from_slice` is ever reached (which then fails harmlessly on malformed JSON) — assert that memory usage/time crosses an expected bound (e.g., via `/proc/self/status VmRSS` sampling) proportional to attacker-controlled `N`, confirming the missing size cap.

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
