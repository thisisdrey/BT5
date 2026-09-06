### Title
Signer Event Receiver Accepts Unauthenticated Remote HTTP POSTs Despite Documented Auth-Token Requirement - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` binds an HTTP listener that accepts `/stackerdb_chunks`, `/new_burn_block`, `/new_block`, and `/proposal_response` POST requests and turns their JSON bodies directly into `SignerEvent`s that are forwarded into the signer's processing pipeline, with **no verification of any authentication credential** on the incoming request. [1](#0-0) 

### Finding Description
The Stacks signer/node integration is documented and configured to require a shared secret (`auth_token` on the node side / `auth_password` on the signer side) so that only the node is trusted to push events to the signer's listening endpoint: [2](#0-1) [3](#0-2) 

However, `SignerEventReceiver::next_event`, which is the transport that actually parses inbound HTTP requests, never inspects any `Authorization` header or shared-secret token. It only checks the URL path and HTTP method before handing the raw body to `process_event`: [4](#0-3) 

`process_event` deserializes the body straight into the target event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`) via `serde_json::from_slice` and immediately converts it into a `SignerEvent` that is forwarded to the signer runloop: [5](#0-4) 

This breaks the "authenticated vs. stored/forwarded" equality: the system design and its configuration surface (`auth_token`/`auth_password`) assert that only a properly-authenticated node can inject events into the signer, but the actual transport code applies no such gate — it fails open. Any host that can reach the signer's bound TCP port (which, per the sample configs, is often `0.0.0.0:30000`, i.e., listening on all interfaces) can POST arbitrary JSON to these endpoints and have it accepted as if it came from the paired Stacks node.

Note: I could not find any authentication check anywhere in `libsigner/src/`, `stacks-signer/src/`, or the relevant sample configs that is actually enforced by the receiver code — the `auth_token`/`auth_password` fields appear to exist only as configuration values with no corresponding verification path in this codebase, per my searches.

### Impact Explanation
This allows an unauthenticated remote party to inject forged `NewBurnBlock`, `NewBlock`, `BlockValidationResponse`, and `StackerDBChunksEvent` data straight into a signer's event stream, which is subsequently consumed by the signer's core logic (out of scope for detailed analysis per the rules, since "the signer decision logic" is excluded — but the injection point itself is squarely in the in-scope `libsigner` transport code). At minimum this is an unauthenticated write of attacker-controlled data into the signer process and an auth-bypass of the documented access control, meeting the "Critical" impact bar (unauthenticated/unauthorized write to state, auth bypass).

### Likelihood Explanation
High. No secret, node key, or special privilege is required — the attacker only needs network reachability to the signer's listen port, which per the shipped sample configs (`0.0.0.0:30000`) is commonly bound to all interfaces. The exploit is a single unauthenticated HTTP POST.

### Recommendation
Add authentication enforcement (e.g., a constant-time comparison of a bearer token / shared secret header) inside `SignerEventReceiver::next_event` (or `process_event`) in `libsigner/src/events.rs` before any event body is parsed and forwarded, matching the `auth_token`/`auth_password` values already present in the configuration files. Reject any request lacking a valid, matching token with an HTTP 401/403 before deserializing the body.

### Proof of Concept
```bash
# Assuming a signer is listening on 127.0.0.1:30000 per sample config,
# with no code-level auth check present:
curl -s -X POST http://127.0.0.1:30000/new_burn_block \
  -H "Content-Type: application/json" \
  -d '{"burn_block_hash":"0x00..","burn_block_height":999999,"reward_recipients":[],"consensus_hash":"0x00..","burn_amount":0}'
```
This forged event is accepted and forwarded into the signer's event channel exactly as if it had come from the legitimately paired, authenticated Stacks node, because `next_event`/`process_event` perform no credential check. [1](#0-0)

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

**File:** sample/conf/mainnet-miner-conf.toml (L391-392)
```text
# and block proposals will fail silently.
auth_token = "<YOUR_AUTH_TOKEN>"
```
