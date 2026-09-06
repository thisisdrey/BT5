### Title
Unauthenticated write to signer process state via forged events on `SignerEventReceiver` HTTP listener - (File: `libsigner/src/events.rs`)

### Summary
The signer's event-receiver HTTP server, which is supposed to only accept event pushes from the paired Stacks node, performs no authentication check on inbound POST requests. Any network peer that can reach the configured listener address can POST a forged `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `NewBlock` payload and have it processed exactly as if it had been sent by the trusted node, because `next_event` dispatches purely on URL path/method.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` binds an HTTP server and, on every request, only checks the request's URL and method before deserializing the JSON body into a `SignerEvent`: [1](#0-0) 

There is no check of any shared secret/auth token, signature, or peer identity anywhere in this dispatch path — `process_event` simply reads the body and deserializes it: [2](#0-1) 

By contrast, the node-side configuration explicitly documents an `auth_token`/`auth_password` pair that is supposed to authenticate this channel between the node and the signer ("Must match" requirement in the signer setup docs): [3](#0-2) 

This equality — "message authenticated by the shared token" vs. "message actually verified by the receiver" — is broken: the token is configured and documented as a security control, but the `SignerEventReceiver` implementation (the actual transport endpoint that receives events) never inspects any header/token to enforce it. This mirrors the reported bug class of "an auth-gate that fails open": the receiving endpoint accepts and processes writes to internal state (forwarded to the signer runloop via `forward_event`) without validating the sender is the legitimate, paired node.

Note: individual message types carry their own downstream cryptographic checks in some cases (e.g., StackerDB chunk signatures are separately verified when messages are converted via `TryInto<SignerEvent<T>>`), but the initial acceptance into the signer's event pipeline itself is unauthenticated at the transport layer, and event types without such embedded checks (e.g., `BurnBlockEvent`, `NewBlock`) are accepted from anyone who can reach the port.

### Impact Explanation
This is a remote, unauthenticated write into a security-critical process's internal event stream. An attacker with network access to the signer's `endpoint` can inject `SignerEvent`s that drive the signer's local state machine (e.g., faked burn-block notifications, faked block-validation responses, or spoofed StackerDB chunk events), independent of the intended `auth_token` control. Depending on how the runloop consumes unauthenticated `NewBurnBlock`/`NewBlock`/`BlockValidationResponse` events, this can influence signer decision timing/state — a data-validation/auth-bypass class issue reaching the signer process. This aligns with "unauthenticated/unauthorized write to state" impact.

### Likelihood Explanation
Exploitability only requires network reachability to the signer's HTTP listener (`config.endpoint`, typically bound to `0.0.0.0` or a LAN-reachable address per sample configs) — no cryptographic material, no node secret, and no privileged role are needed. This is directly reachable by any unprivileged remote party who can route to the port; it is not a volumetric attack, but a single well-formed POST is sufficient.

### Recommendation
Enforce the documented `auth_token`/`auth_password` check inside `SignerEventReceiver::next_event` (or an earlier middleware layer) by validating a shared-secret header (e.g., `Authorization`) on every incoming request before dispatching to `process_event`, rejecting any request that lacks a valid, constant-time-compared token. Alternatively/additionally, bind the listener to loopback-only by default and document that it must never be exposed beyond the local node, but the primary fix should be enforcing the existing auth token at the transport layer since the codebase already treats it as the intended control.

### Proof of Concept
1. Configure and run a `stacks-signer` per `docs/signing.md`, noting `endpoint = "0.0.0.0:30000"` (or any non-loopback bind).
2. From a separate, unauthenticated host with network access to port 30000, craft an HTTP POST to `http://<signer-host>:30000/new_burn_block` (or `/stackerdb_chunks`, `/proposal_response`, `/new_block`) with a JSON body matching the `BurnBlockEvent`/`StackerDBChunksEvent`/`BlockValidateResponse` schema, without providing the node's configured `auth_token`.
3. Observe that `SignerEventReceiver::next_event` (`libsigner/src/events.rs:413-458`) processes the request purely based on URL/method, deserializes it via `process_event` (`libsigner/src/events.rs:519-542`), and forwards it into the signer runloop via `forward_event`, with no rejection due to a missing/invalid auth token. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** docs/signing.md (L31-59)
```markdown
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```

### 2. Configure the Signer

Use [`mainnet-signer-conf.toml`](../sample/conf/signer/mainnet-signer-conf.toml) as a starting point.
Key settings:

```toml
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
