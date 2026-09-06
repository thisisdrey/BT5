### Title
Signer event-receiver HTTP endpoint accepts unauthenticated POSTs, allowing forged `BlockValidationResponse`/`NewBurnBlock`/`NewBlock`/`StackerDBChunksEvent` messages to be injected into the signer runloop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver`, the HTTP server the signer runs to receive event-observer pushes from its paired Stacks node, processes any POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` without ever checking a shared secret, bearer token, or peer identity. The Maia report's bug class is "a check that should gate a privileged/authenticated action is missing, so state gets updated as if it came from a trusted party." Here the equality that should hold — "this event genuinely originated from my paired node" — is never checked before the event is decoded and forwarded into the signer's decision loop.

### Finding Description
`EventReceiver::next_event` for `SignerEventReceiver<T>` dispatches purely on URL path and HTTP method: [1](#0-0) 

There is no authentication/authorization check anywhere in this dispatch — no `Authorization` header, no shared `auth_token`/`auth_password` comparison, no source-address allow-list. `process_event` simply reads the request body and JSON-deserializes it into the target event type: [2](#0-1) 

The deployment documentation and sample configs describe an `auth_token`/`auth_password` that is supposed to gate "authentication for signer communication" between node and signer: [3](#0-2) [4](#0-3) 

However, within the in-scope `libsigner` transport code that actually implements the signer-side HTTP receiver (`bind`/`next_event`), no such token is ever read from the incoming request or compared against a configured value. `bind()` simply opens an `HttpServer` on the configured listener address with no auth middleware: [5](#0-4) 

Once decoded, the event is unconditionally forwarded to the signer runloop: [6](#0-5) 

This mirrors the report's fault pattern precisely: a message that should only be actionable when it comes from an authenticated/authorized source (the paired node) is accepted and acted upon whenever it merely arrives on the right URL. Note that `StackerDBChunksEvent` payloads embed `StackerDBChunkData`, which *does* carry a per-chunk signature that downstream code can verify against the expected slot signer — but `BlockValidateResponse`, `NewBurnBlock`, and `NewBlock` events carry no such per-message signature at all in this layer, so for those three message types there is no cryptographic fallback once the (missing) transport-level auth check is bypassed.

### Impact Explanation
If the signer's event-receiver socket is reachable by anything other than strictly localhost/loopback-only traffic (e.g., bound to a non-loopback interface, reachable via port-forwarding, container networking, or a misconfigured firewall — a realistic operational scenario since the bind address is operator-configurable), any unauthenticated party can:
- Inject a forged `BlockValidationResponse` to influence the signer's view of block validity for a proposal it never actually validated.
- Inject a forged `NewBurnBlock`/`NewBlock` event, corrupting the signer's view of chain tip/burn-height state used to drive its decision logic and timers.
This is an unauthenticated write into signer-local state via a network-reachable endpoint with no cryptographic or shared-secret gate at the transport layer, which corresponds to the "unauthenticated/unauthorized write to state" and "steering a node off the tip via false data" impact classes.

### Likelihood Explanation
Likelihood depends entirely on whether operators bind this listener to a non-loopback address (the field is a configurable `SocketAddr`, and sample configs show it defaulting to `127.0.0.1`, mitigating remote exposure by default). Given the explicit existence of an `auth_token`/`auth_password` configuration knob referenced in documentation and sample configs, it strongly suggests the design intent was to authenticate this channel, but the `libsigner` transport code in scope (`events.rs`) contains no enforcement of it, so any deployment that exposes the receiver (intentionally or via misconfiguration) is unauthenticated at this layer, unlike what the documented `auth_token` implies.

### Recommendation
Enforce the documented `auth_token`/`auth_password` check inside `SignerEventReceiver::next_event`/`process_event` before decoding and forwarding any event — e.g., require and constant-time-compare an `Authorization` header against the configured secret, rejecting the request otherwise, consistent with how other in-scope RPC handlers gate privileged actions.

### Proof of Concept
1. Start a signer with `SignerEventReceiver::bind` listening on an address reachable to the attacker (e.g., `0.0.0.0:30000` per an intentionally or accidentally permissive config).
2. From an arbitrary unauthenticated host, send:
```
POST /new_burn_block HTTP/1.1
Host: <signer>:30000
Content-Type: application/json
Content-Length: <n>

{"burn_height": 999999, "burn_header_hash": "...", "consensus_hash": "...", ...}
```
3. `next_event` routes this straight to `process_event::<T, BurnBlockEvent>` with no auth check, per `libsigner/src/events.rs:441-442`, and the resulting `SignerEvent::NewBurnBlock` is forwarded into the signer runloop as if it had come from the paired node. [7](#0-6)

### Citations

**File:** libsigner/src/events.rs (L401-408)
```rust
    /// Start listening on the given socket address.
    /// Returns the address that was bound.
    /// Errors out if bind(2) fails
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L413-458)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

            if request.url() == "/status" {
                request
                .respond(HttpResponse::from_string("OK"))
                .expect("response failed");
                return Ok(SignerEvent::StatusCheck);
            }

            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
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
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
```

**File:** libsigner/src/events.rs (L466-490)
```rust
    /// Forward an event
    /// Return true on success; false on error.
    /// Returning false terminates the event receiver.
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

**File:** sample/conf/testnet-signer.toml (L45-47)
```text
[connection_options]
# WARNING: Must match the signer binary's `auth_password`.
auth_token = ""
```

**File:** sample/conf/testnet-miner-conf.toml (L73-78)
```text
# ============================================================
# [connection_options] - Authentication for signer communication
# ============================================================
[connection_options]
# WARNING: Must match the signer's auth_password.
auth_token = "<YOUR_AUTH_TOKEN>"
```
