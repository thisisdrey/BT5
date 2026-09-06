This confirms the finding: `SignerEventReceiver::next_event` and `process_event` perform zero authentication checks on incoming HTTP requests — no auth token, no signature, no source IP check — before deserializing the body and converting it into a `SignerEvent`, which is then forwarded verbatim to the signer runloop.

### Title
Unauthenticated `/proposal_response` endpoint lets any TCP peer inject forged `BlockValidationResponse` events into the signer runloop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches any POST to `/proposal_response` straight into `process_event::<T, BlockValidateResponse>` with no authentication, source-address check, or signature verification of any kind. Any TCP client that can reach the signer's event-listener port (which the sample configs explicitly bind to `0.0.0.0`) can POST an arbitrary JSON body and have it delivered as a trusted `SignerEvent::BlockValidationResponse` to the signer runloop.

### Finding Description
The claimed equality — "`BlockValidationResponse` the runloop trusts as the node's own opinion" == "a response actually produced by the local trusted node process" — is broken. In `next_event` (`libsigner/src/events.rs:413-458`), the dispatch is based purely on `request.url()`: [1](#0-0) 
When the URL is `/proposal_response`, `process_event::<T, BlockValidateResponse>(request)` is invoked. In `process_event` (`libsigner/src/events.rs:519-542`), the function reads the raw body, deserializes it as JSON into `BlockValidateResponse`, and directly converts it via `TryInto<SignerEvent<T>>`: [2](#0-1) 
There is no check anywhere in this call chain for an `auth_token`/`auth_password`, a signature, or the peer's source address — the `auth_password`/`auth_token` mechanism documented in `sample/conf/signer/mainnet-signer-conf.toml:45-50` and `sample/conf/mainnet-signer.toml:36-38` governs the *node's RPC/events_observer delivery side*, not the *signer's inbound HTTP listener* implemented in `SignerEventReceiver`. `SignerEventReceiver::bind` (`libsigner/src/events.rs:404-408`) simply calls `HttpServer::http(listener)` with no auth middleware, and the sample signer config even documents `endpoint = "0.0.0.0:30000"` (`sample/conf/signer/mainnet-signer-conf.toml:39`), meaning this listener is explicitly intended to be reachable beyond loopback in some deployments. Any attacker who can open a TCP connection to that port can send a crafted HTTP POST with a valid `BlockValidateResponse` JSON body and it will be accepted, converted into `SignerEvent::BlockValidationResponse`, and forwarded via `forward_event` (`libsigner/src/events.rs:469-490`) to the signer's runloop channel exactly as if the trusted local node had produced it.

### Impact Explanation
This is an unauthenticated write into the signer's decision-making stream: an attacker can inject a forged block-validation verdict (accept or reject) for any block, which the signer runloop consumes as ground truth from its paired node. Because this requires no secret, no signature, and no privileged role — only TCP reachability to the bind address — it matches the "unauthenticated/unauthorized write to state" Critical category, repeatable per message with no rate limiting visible in this path.

### Likelihood Explanation
The only precondition is TCP reachability to the signer's event-listener port. Given the sample configuration explicitly sets `endpoint = "0.0.0.0:30000"` for the signer's own listener (`sample/conf/signer/mainnet-signer-conf.toml:39`), operators following the documented reference config would expose this port beyond localhost. Attacker cost is a single crafted HTTP POST with valid JSON; the action is trivially repeatable.

### Recommendation
Add authentication to `SignerEventReceiver`'s inbound HTTP listener — e.g., require and validate the same shared secret/`auth_token` mechanism used for the node's RPC/events_observer, or restrict accepted source addresses, before calling `process_event` in `next_event` (`libsigner/src/events.rs:436-457`). At minimum, document/enforce binding this listener to loopback only, and reject requests missing a valid shared-secret header.

### Proof of Concept
1. In a Rust test analogous to `test_simple_signer`/`test_status_endpoint` in `libsigner/src/tests/mod.rs:93-230`, spawn `Signer::spawn` with a `SignerEventReceiver` bound to a test port, and add an `out_channel` consumer.
2. From a plain `TcpStream::connect` (no node-side credentials, no HMAC), send:
   ```
   POST /proposal_response HTTP/1.1
   Host: 127.0.0.1:PORT
   Content-Type: application/json
   Content-Length: <n>

   { ...crafted BlockValidateResponse JSON... }
   ```
3. Assert that the consumer channel receives `SignerEvent::BlockValidationResponse(<attacker-chosen content>)`, proving the forged verdict was accepted and forwarded without any authentication check, mirroring the existing `test_simple_signer` pattern but targeting `/proposal_response` from an unauthenticated raw socket instead of the trusted node.

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

**File:** libsigner/src/events.rs (L524-542)
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

    let signer_event: SignerEvent<T> = json_event.try_into()?;

    Ok(signer_event)
}
```
