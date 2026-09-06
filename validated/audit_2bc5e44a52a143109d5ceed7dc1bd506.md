Based on the code inspection, this vulnerability is valid and confirmed by the source.

### Title
Unauthenticated `/proposal_response` HTTP endpoint allows forged `BlockValidateResponse` injection into the signer's event pipeline - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver::next_event` handler routes any POST to `/proposal_response` straight into `process_event::<T, BlockValidateResponse>` with no signature, secret, or origin check, then `forward_event` relays it unmodified to the signer runloop as `SignerEvent::BlockValidationResponse`. Any TCP client that can reach the signer's event-receiver port can therefore inject a fabricated "the node validated/rejected block X" verdict that the signer treats as authentic.

### Finding Description
The broken equality is: the runloop should only ever observe a `BlockValidateResponse` that was actually produced by *this node's* `postblock_proposal` validation logic and pushed by *this node's* internal event dispatcher. Instead, `next_event` dispatches purely on the HTTP method/URL: [1](#0-0) 
`process_event` reads the raw body and deserializes it directly into `BlockValidateResponse` via `serde_json::from_slice`, with no header, secret, or peer-identity check performed anywhere in the function: [2](#0-1) 
The `TryFrom<BlockValidateResponse> for SignerEvent<T>` conversion is a pure pass-through with no validation of provenance: [3](#0-2) 
`forward_event` then sends this event unchanged to every registered downstream channel (i.e., the runloop): [4](#0-3) 
Corroborating this, the node's own event-dispatcher client that normally sends these payloads carries no authentication token, secret header, or signature over the request — it is a bare HTTP POST — confirmed by the absence of any `Authorization`/secret/token handling in `stacks-node/src/event_dispatcher.rs`, and `docs/event-dispatcher.md` documents the `/proposal_response` payload format with no mention of any authentication mechanism. There is no guard anywhere in this chain (no HMAC, no shared secret, no source-IP allowlist enforced in code) tying an inbound `/proposal_response` POST to an actual `postblock_proposal` call the node itself made.

### Impact Explanation
Any party with TCP reachability to the signer's event-receiver bind address can POST a synthetic `BlockValidateOk`/`BlockValidateReject` JSON body for a block hash that was never submitted to or validated by `postblock_proposal`. This is delivered unchanged into the signer runloop's event channel as `SignerEvent::BlockValidationResponse`, i.e., an unauthenticated write into the signer's internal decision-input state. This is repeatable per message (one POST per forged verdict) and requires no cryptographic material, matching the "Critical - unauthenticated/unauthorized write to state" category, scoped strictly to the transport/authentication gap (the downstream vote/signature logic that consumes this event is explicitly out of scope for this finding).

### Likelihood Explanation
Precondition is simply TCP reachability to the signer's configured event-receiver listener (the address the node's `[[events_observer]]` `endpoint` points at). No secret, key, StackerDB slot, or admin role is required — a bare `curl -X POST` with a JSON body is sufficient. Cost to the attacker is a single HTTP request, fully repeatable at will.

### Recommendation
Add an authentication/authorization gate on the signer's event-receiver HTTP server (e.g., a shared secret / HMAC header verified in `next_event`/`process_event` before deserializing, or restrict the listener to a loopback/unix-socket bound to the paired node process) so that `/proposal_response` (and the other event endpoints) can only be driven by the node process that owns the corresponding `postblock_proposal` call, not by an arbitrary TCP peer.

### Proof of Concept
```rust
// libsigner/src/events.rs (or an integration test crate)
// 1. Bind a SignerEventReceiver<T> on 127.0.0.1:0, obtain the port.
// 2. Register a std::sync::mpsc channel via add_consumer.
// 3. Spawn a thread running receiver.next_event() -> forward_event(ev) once.
// 4. From a plain std::net::TcpStream (no auth headers, no node involvement),
//    send:
//      POST /proposal_response HTTP/1.1
//      Host: 127.0.0.1:<port>
//      Content-Type: application/json
//      Content-Length: <n>
//
//      {"result":"Ok","block": <hex-encoded fake NakamotoBlock>, "cost": {...}, "size": 0}
// 5. Assert the mpsc receiver yields
//      SignerEvent::BlockValidationResponse(BlockValidateResponse::Ok(_))
//    for a block hash that was never passed through postblock_proposal,
//    proving the forged verdict was accepted and forwarded unchanged.
```

### Citations

**File:** libsigner/src/events.rs (L436-441)
```rust
            debug!("Processing {} event", request.url());
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
```

**File:** libsigner/src/events.rs (L469-490)
```rust
    fn forward_event(&mut self, ev: SignerEvent<T>) -> bool {
        if self.out_channels.is_empty() {
            // nothing to do
            error!("No channels connected to event receiver");
            false
        } else if self.out_channels.len() == 1 {
            // avoid a clone
            if let Err(e) = self.out_channels[0].send(ev) {
                error!("Failed to send to signer runloop: {:?}", &e);
                return false;
            }
            true
        } else {
            for (i, out_channel) in self.out_channels.iter().enumerate() {
                if let Err(e) = out_channel.send(ev.clone()) {
                    error!("Failed to send to signer runloop #{}: {:?}", i, &e);
                    return false;
                }
            }
            true
        }
    }
```

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

**File:** libsigner/src/events.rs (L627-635)
```rust
impl<T: SignerEventTrait> TryFrom<BlockValidateResponse> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(block_validate_response: BlockValidateResponse) -> Result<Self, Self::Error> {
        Ok(SignerEvent::BlockValidationResponse(
            block_validate_response,
        ))
    }
}
```
