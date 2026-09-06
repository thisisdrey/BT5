### Title
Unauthenticated event ingestion in `SignerEventReceiver` allows forging node-to-signer events (StackerDB chunks, block-validation responses, burn-block/new-block notifications) - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` (the signer-side HTTP endpoint that the Stacks node POSTs events to) performs **no authentication check whatsoever** on incoming requests. It only inspects the URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown`) and the HTTP method, then deserializes the body directly into a `SignerEvent` and forwards it into the signer's runloop. Anyone who can reach this listening socket can inject forged events as if they came from the trusted node, exactly analogous to the Jenkins SAML bug where a URL meant to be reachable only in one trusted, narrow context was instead reachable/actionable from anywhere.

### Finding Description
`SignerEventReceiver::bind` opens a plain HTTP server on the signer's configured `endpoint` [1](#0-0) . `next_event` then dispatches purely by path/method with no credential check: [2](#0-1) 

`process_event` reads the raw body and deserializes it directly into the target event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`) with no signature, token, or origin check at all — the only gate is JSON-schema conformance: [3](#0-2) 

Contrast this with the *outgoing* direction: the signer client authenticates to the node's RPC using `auth_password`/`auth_token` (`stacks-signer/src/client/stacks_client.rs`, `stacks-signer/src/config.rs`), and the node's own `/v3/block_proposal`, `/v3/blocks/simulate`, etc. RPC handlers enforce a shared-secret `authorization` header check before accepting privileged requests (e.g. `stackslib/src/net/api/postblock_proposal.rs`). No equivalent check exists on the *inbound* side, where the node pushes events to the signer. The intended trust model — "only the paired node may deliver these events" — is not enforced by any credential; it relies solely on network reachability, which the project's own sample configuration undermines by binding the signer's event endpoint to `0.0.0.0` [4](#0-3) .

This is directly analogous to the Jenkins SAML advisory: a mechanism intended to be trusted/whitelisted for a very narrow, specific caller (the node) is instead wide open to any caller that can reach the endpoint, because the actual authenticity check that should gate it was never implemented (Jenkins disabled CSRF too broadly; here, no auth is applied at all).

### Impact Explanation
Any remote, unauthenticated party that can reach the signer's bound HTTP port can:
- POST a forged `/stackerdb_chunks` body to inject an arbitrary `StackerDBChunksEvent` into the signer's event stream.
- POST a forged `/proposal_response` to inject an arbitrary `BlockValidateResponse` as if the paired node had validated (or rejected) a block proposal.
- POST forged `/new_burn_block` or `/new_block` bodies to inject arbitrary burn-block/tip notifications.

These events are fed straight into the signer's `run_one_pass`/runloop as authentic node-originated data. Because ingestion has zero authentication, this breaks the fundamental "node ↔ signer" trust boundary at the transport layer — a source of state the signer implicitly treats as authenticated-but-isn't (the equality the Jenkins-report class targets: "authenticated vs. actually verified"). This falls under "unauthenticated/unauthorized write to state" via network-reachable injection into the signer's decision-input stream. If the listener is reachable beyond localhost (as encouraged by the documented `0.0.0.0` sample binding), this is a purely network-based attack requiring no secrets, no node key, and no privileged role.

### Likelihood Explanation
Likelihood is high wherever the signer's event-receiver socket is reachable from an untrusted network segment. The project's own documentation sample configures `endpoint = "0.0.0.0:30000"` for the signer [4](#0-3) , and `Signer::spawn`/`SignerEventReceiver::bind` place no restriction on the bind address or any TLS/auth requirement [5](#0-4) . No attacker-controlled secret or timing dependency is needed — a single crafted HTTP POST suffices, and the code path is reached unconditionally for any request matching a recognized URL.

### Recommendation
Add authentication to `SignerEventReceiver`'s inbound HTTP handling — e.g., require and verify a shared secret/HMAC header (mirroring the `auth_token`/`authorization` check already used for outgoing signer→node RPC calls) before calling `process_event`/`ack_dispatcher`, and reject unauthenticated requests with 401 rather than forwarding them into the runloop. At minimum, document and default to binding the event receiver to loopback only, and enforce that binding in code rather than relying on operator configuration.

### Proof of Concept
1. Configure/observe that a stacks-signer instance binds its event receiver per the documented sample (`endpoint = "0.0.0.0:30000"`), so the port is reachable off-host.
2. From an unauthenticated remote client, send:
   ```
   POST /proposal_response HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   { ... forged BlockValidateResponse JSON ... }
   ```
3. `SignerEventReceiver::next_event` matches on `/proposal_response`, calls `process_event::<T, BlockValidateResponse>(request)` with no credential check, and successfully forwards the forged `SignerEvent` to the signer's runloop channel (`forward_event`), identical to what a legitimate node POST would produce [6](#0-5) .

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

**File:** docs/signing.md (L43-49)
```markdown
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```
```

**File:** libsigner/src/runloop.rs (L223-236)
```rust
        let bind_port = bind_addr.port();
        event_receiver.bind(bind_addr)?;
        let stop_signaler = event_receiver.get_stop_signaler()?;
        let mut ret_stop_signaler = event_receiver.get_stop_signaler()?;

        // start a thread for the event receiver
        let event_thread = thread::Builder::new()
            .name(format!("event_receiver:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || event_receiver.main_loop())
            .map_err(|e| {
                error!("EventReceiver failed to start: {:?}", &e);
                EventError::FailedToStart
            })?;
```
