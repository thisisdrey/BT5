## Finding

### Title
Signer's `SignerEventReceiver` HTTP endpoint accepts unauthenticated event pushes, allowing forged `BlockValidateResponse` / `BurnBlockEvent` / `StackerDBChunksEvent` injection - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver` HTTP listener that the stacks-signer binds to receive events pushed by the node (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) performs no authentication or authorization check on inbound POST requests. Any party with network access to this listening port can send a forged event directly into the signer's processing pipeline, exactly analogous to kgateway's xDS interface accepting unauthenticated clients and exposing/altering control-plane data.

### Finding Description
`SignerEventReceiver::next_event` dispatches based solely on URL path and HTTP method, with no header/token check before calling `process_event`: [1](#0-0) 

`process_event` simply reads the body and deserializes it into the target JSON event type — there is no signature or shared-secret verification gate at this layer: [2](#0-1) 

Two of the three dispatched event types are **not individually signed or otherwise cryptographically bound**: `BlockValidateResponse` (`/proposal_response`) and `BurnBlockEvent` (`/new_burn_block`). Only `StackerDBChunksEvent` chunks carry a `sig` field that a downstream consumer *could* re-verify, but the receiver itself performs no such check before forwarding the event: `forward_event` unconditionally sends the parsed event into the signer runloop channel: [3](#0-2) 

By contrast, the *other* direction of signer↔node communication (signer calling the node's `/v3/block_proposal` endpoint) does enforce an `auth_token`/`auth_password` check, as referenced throughout `stackslib/src/net/api/postblock_proposal.rs` and documented in the operator guides: [4](#0-3) 

The node's outbound event-dispatch path (`event_dispatcher/worker.rs::make_http_request`) that actually pushes events to the signer's listener adds only a `Connection: close` header — no `Authorization`/token header is attached to these outbound POSTs, confirming that the signer-side listener is not designed to expect or check any credential on this channel: [5](#0-4) 

This is the direct structural analog of the kgateway finding: a control/data distribution interface (xDS ⇔ signer event receiver) that any network-adjacent, unauthenticated client can talk to, obtaining or — in this case, worse, actively injecting — sensitive control data.

### Impact Explanation
An attacker with network reach to the signer's event-receiver port (bound per `[[events_observer]] endpoint`, configurable beyond loopback) can:
- POST a forged `BurnBlockEvent` to `/new_burn_block`, injecting a false view of burnchain state into the signer.
- POST a forged `BlockValidateResponse` to `/proposal_response`, spoofing the node's block-validation verdict as seen by the signer.
- POST arbitrary `StackerDBChunksEvent` data to `/stackerdb_chunks` without any check that it actually originated from the node.

Since the signer's `main_loop` unconditionally forwards whatever is received to the runloop channel, this is an unauthenticated write of forged control data into signer-observed state, matching the "Critical: unauthenticated/unauthorized write to state" bar. The maintainers themselves acknowledge the risk of exposing this interface without "additional proper security checks", in the `SpawnedSigner::new` startup warning: [6](#0-5) 

### Likelihood Explanation
Likelihood depends on network exposure of the configured `endpoint`. Default sample configs bind to `127.0.0.1:30000`, limiting exposure to local processes, but the field is user-configurable and the codebase provides no enforcement preventing binding to a non-loopback address, nor any authentication fallback if it is. Any signer deployment where this endpoint is reachable from an adjacent network (matching the original advisory's `AV:A` vector) is directly exploitable with a single crafted HTTP POST.

### Recommendation
Add mandatory authentication (e.g., shared-secret / bearer-token check reusing the existing `auth_token`/`auth_password` mechanism, or per-event signature verification) inside `SignerEventReceiver::next_event` / `process_event` in `libsigner/src/events.rs` before parsing and forwarding any event, mirroring the check already applied on the reverse (signer→node) channel in `postblock_proposal.rs`.

### Proof of Concept
1. Configure/observe a signer's `[[events_observer]] endpoint`.
2. From an unauthenticated peer with network access to that port, send:
```
POST /new_burn_block HTTP/1.1
Host: <signer_endpoint>
Content-Type: application/json
Content-Length: <n>

{ ...forged BurnBlockEvent JSON... }
```
3. The request is accepted, deserialized, and forwarded into the signer runloop with no credential check, as shown by the dispatch/parse path in `libsigner/src/events.rs:437-458` and `libsigner/src/events.rs:519-542`.

Note: I was unable to fully trace whether any downstream consumer of `SignerEvent` (e.g. in `stacks-signer/src/v0` runloop logic) performs additional out-of-band validation of `BlockValidateResponse`/`BurnBlockEvent` contents beyond what's shown here, since deep signer-decision-logic internals are out of scope per the rules; the finding is scoped strictly to the missing authentication at the `libsigner` transport/reception layer itself.

### Citations

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

**File:** docs/signing.md (L43-59)
```markdown
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```

### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```

**File:** stacks-node/src/event_dispatcher/worker.rs (L347-356)
```rust
        loop {
            let mut request = StacksHttpRequest::new_for_peer(
                peerhost.clone(),
                "POST".into(),
                url.path().into(),
                HttpRequestContents::new().payload_json_bytes(Arc::clone(&data.payload_bytes)),
            )
            .unwrap_or_else(|_| panic!("FATAL: failed to encode infallible data as HTTP request"));
            request.add_header("Connection".into(), "close".into());
            match send_http_request(host, port, request, data.timeout) {
```

**File:** stacks-signer/src/lib.rs (L124-132)
```rust
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
