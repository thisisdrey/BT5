This endpoint is reachable by whoever the signer operator configures to send events (typically the paired stacks-node's event dispatcher), and this binding is an operator/config decision, not something the wire protocol enforces — but per the question's threat model, an unprivileged remote party that can connect to the bound listener can send arbitrary POST bodies. Given `process_event` in `libsigner/src/events.rs`, I can confirm the finding.

### Title
Unbounded body read in `SignerEventReceiver::process_event` allows single-request memory exhaustion DoS - (File: libsigner/src/events.rs)

### Summary
`process_event` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` with no length limit, before any `serde_json` parsing or authentication occurs. Any peer able to connect to the signer's event-listener port and issue a POST to `/stackerdb_chunks`, `/proposal_response`, or `/new_burn_block` (or `/new_block`) can send an arbitrarily large body (bounded only by `Content-Length`/chunked transfer, not validated against any cap), forcing the process to allocate memory proportional to the attacker-supplied body size.

### Finding Description
In `libsigner/src/events.rs`, `SignerEventReceiver::next_event` (lines 413-459) dispatches incoming HTTP POST requests by URL to `process_event::<T, E>(request)` without any prior size check. `process_event` (lines 519-542) does:
```rust
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
``` [1](#0-0) 

There is no `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN`-style cap applied to the reader before or during this read. `tiny_http`'s `Request::as_reader()` returns a reader that will happily read as many bytes as the client sends (whether declared via `Content-Length` or streamed via chunked encoding); `read_to_string` will keep growing the `String` buffer to accommodate all of it. The equality the question posits — "bytes read into `body` == a validated maximum length" — does not hold: the actual bound is "bytes read into `body` == however many bytes the client chose to send," constrained only by available memory and TCP throughput, not by any application-level cap. Deserialization via `serde_json::from_slice` only happens after the full body is already resident in memory, so oversized-body protection at the JSON-parsing layer is irrelevant to the allocation itself.

Downstream per-field caps that exist elsewhere in the codebase (e.g., `BLOCK_RESPONSE_DATA_MAX_SIZE` used in `BlockProposalData::consensus_deserialize`, `libsigner/src/events.rs:183`) only bound *nested* codec fields after the outer JSON has already been fully materialized in memory — they do not gate the initial HTTP body read.

### Impact Explanation
A single crafted POST with a very large (or infinite/chunked) body causes the signer process's event-receiver thread to attempt to allocate memory proportional to the attacker-controlled body size, up to exhausting available RAM and crashing or hanging the signer process — an unauthenticated, single-message DoS against the signer's event listener. This matches the Critical category ("remote crash/unauthenticated DoS from few messages"). It is trivially repeatable per connection.

### Likelihood Explanation
No authentication, signature, or secret is required to reach `process_event` — the check performed before dispatch is only URL matching and HTTP method (`request.method() != &HttpMethod::Post`), not sender identity. Precondition is only that the attacker can open a TCP connection to the bound listener address and send a valid HTTP POST with an oversized body; cost is a single request. Whether this is remotely reachable by an "unprivileged internet attacker" depends on the deployment's network exposure of the signer's event-receiver bind address (this is operator-configured and not something enforced in the protocol/code itself), but within the stated threat model of "any remote party who can connect to a node's ... RPC port and send arbitrary bytes," the code path itself provides no defense once a connection is established.

### Recommendation
Impose an explicit maximum body size check before/while reading in `process_event` — e.g., inspect and cap based on the `Content-Length` header, and/or use a bounded reader (`Read::take(MAX_LEN)`) instead of unconditionally calling `read_to_string` on the raw reader, rejecting requests that exceed the cap with an HTTP 413-equivalent response.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
// Spin up a SignerEventReceiver, bind it, then from a separate thread:
// 1. Connect a TcpStream to the bound address.
// 2. Write: "POST /new_burn_block HTTP/1.1\r\nHost: x\r\nContent-Length: 5000000000\r\n\r\n"
//    followed by streaming gigabytes of filler bytes (or a chunked-encoded infinite body).
// 3. Assert that the signer process's memory usage grows unbounded / the process OOMs,
//    or instrument process_event to show `body.len()` growing past any sane cap
//    (e.g., >> BLOCK_RESPONSE_DATA_MAX_SIZE) before serde_json::from_slice is ever invoked
//    at libsigner/src/events.rs:536.
``` [2](#0-1)

### Citations

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
