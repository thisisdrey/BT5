### Title
Unauthenticated forged chain-tip injection via `/new_burn_block` and `/new_block` on the signer's event port - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` dispatches any POST to `/new_burn_block` or `/new_block` straight into `process_event::<T, BurnBlockEvent>` / `process_event::<T, StacksBlockEvent>`, which JSON-deserialize the body and pass it through a `TryFrom` that does pure field-copying with no cryptographic check, signature, shared secret, or peer-identity check of any kind. Any TCP peer that can reach the bound listener can therefore make the signer runloop believe an attacker-chosen burn height, consensus hash, block height, and transaction set is the node's authoritative chain tip.

### Finding Description
The listener is a plain `tiny_http::HttpServer` bound in `SignerEventReceiver::bind` [1](#0-0) . In `next_event`, the only routing performed is a URL string match; there is no check of the request's remote peer address, no shared-secret/auth header validation, and no signature check before dispatch to `process_event` [2](#0-1) .

`process_event` simply reads the body, ACKs the request, deserializes JSON into `E`, and calls `.try_into()` [3](#0-2) . The `TryFrom<BurnBlockEvent>` and `TryFrom<StacksBlockEvent>` implementations copy every field from the parsed JSON directly into `SignerEvent::NewBurnBlock`/`SignerEvent::NewBlock` with zero validation against actual node state [4](#0-3) [5](#0-4) . The resulting event is forwarded unconditionally to the runloop channel via `forward_event` [6](#0-5) .

The signer's `auth_password` / node's `auth_token` pairing referenced throughout the sample configs only authenticates the *signer-as-client* calling the *node's* RPC endpoints (e.g. block-proposal submission); it plays no role in authenticating inbound POSTs to the signer's own event-listener port [7](#0-6) . No `remote_addr`/`peer_addr` check exists anywhere in `libsigner/src/events.rs`. The sample/reference configs bind this listener to `0.0.0.0:30000` [8](#0-7) , and the code itself acknowledges the risk at startup: `SpawnedSigner::new` logs a warning that communicating with the node "as this could potentially expose sensitive data or functionalities to security risks if additional proper security checks are not integrated" [9](#0-8) , confirming there is no built-in mitigation and the security boundary is left entirely to network-level isolation chosen by the operator.

### Impact Explanation
Any party able to reach the signer's bound event port (which per the sample/reference configs may be `0.0.0.0`, i.e. all interfaces) can inject a `SignerEvent::NewBurnBlock` or `SignerEvent::NewBlock` carrying entirely attacker-chosen `burn_height`, `consensus_hash`, `block_height`, `block_id`, and `transactions`. This steers the signer runloop's view of chain state away from what the node's real, validated event-observer subsystem emitted, satisfying the "steering a node off the tip via forged/false data" class of impact. The attack is trivially repeatable (one crafted HTTP POST per forged event) and requires no signature, key, or credential.

### Likelihood Explanation
Preconditions are exactly as stated: the attacker needs only network reachability to the signer's event-listener TCP port, with no privileged role, no RPC secret, and no key material — this matches the reference/sample deployment configs which bind the listener to `0.0.0.0` [8](#0-7) . Cost per exploit attempt is a single unauthenticated HTTP POST, and it is fully repeatable. The likelihood is entirely a function of network exposure/firewalling of that port, which is outside libsigner's own code but is explicitly flagged by the project's own startup warning as an unmitigated risk when the listener is reachable from outside the local/subnet trust boundary.

### Recommendation
Add authentication to the signer's event-listener HTTP server (e.g., require and verify a shared secret/HMAC header matching the node's `auth_token`, or bind by default to loopback-only and require explicit opt-in plus mutual authentication for non-local bindings) before dispatching `/new_burn_block` and `/new_block` (and other POST routes) into `process_event`, in `libsigner/src/events.rs`'s `next_event`/`process_event`.

### Proof of Concept
1. Start a `SignerEventReceiver::<T>` and `bind` it to `127.0.0.1:0` (or `0.0.0.0:<port>`), register a consumer channel via `add_consumer`.
2. From a separate `TcpStream`, send a raw HTTP POST to `/new_block` with a JSON body for `StacksBlockEvent` containing fabricated `index_block_hash`, `consensus_hash`, `block_height`, and a `transactions` array, mirroring the `SignerStopSignaler::send` raw-HTTP construction pattern already used in this file [10](#0-9) .
3. Call `next_event()` and assert it returns `Ok(SignerEvent::NewBlock { block_id, consensus_hash, block_height, transactions, .. })` with fields exactly equal to the attacker-supplied JSON values, confirming no validation occurred.
4. Repeat against `/new_burn_block` with a fabricated `BurnBlockEvent` body and assert `SignerEvent::NewBurnBlock { burn_height, consensus_hash, .. }` matches the forged input.

### Citations

**File:** libsigner/src/events.rs (L382-394)
```rust
        if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
            // We need to send actual data to trigger the event receiver
            let body = "Yo. Shut this shit down!".to_string();
            let req = format!(
                "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
                self.local_addr,
                body.len(),
                body
            );
            if let Err(e) = stream.write_all(req.as_bytes()) {
                error!("Failed to send shutdown request: {}", e);
            }
        }
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L436-448)
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
            } else {
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

**File:** libsigner/src/events.rs (L637-649)
```rust
impl<T: SignerEventTrait> TryFrom<BurnBlockEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(burn_block_event: BurnBlockEvent) -> Result<Self, Self::Error> {
        Ok(SignerEvent::NewBurnBlock {
            burn_height: burn_block_event.burn_block_height,
            received_time: SystemTime::now(),
            burn_header_hash: burn_block_event.burn_block_hash,
            consensus_hash: burn_block_event.consensus_hash,
            parent_burn_block_hash: burn_block_event.parent_burn_block_hash,
        })
    }
}
```

**File:** libsigner/src/events.rs (L708-720)
```rust
impl<T: SignerEventTrait> TryFrom<StacksBlockEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(block_event: StacksBlockEvent) -> Result<Self, Self::Error> {
        Ok(SignerEvent::NewBlock {
            signer_sighash: block_event.signer_signature_hash,
            block_id: block_event.index_block_hash,
            consensus_hash: block_event.consensus_hash,
            block_height: block_event.block_height,
            transactions: block_event.transactions,
        })
    }
}
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L45-50)
```text
# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```

**File:** stacks-signer/src/lib.rs (L125-132)
```rust
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
