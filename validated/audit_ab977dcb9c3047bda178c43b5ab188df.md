### Title
Unauthenticated event ingestion in `SignerEventReceiver` allows any unprivileged network peer to inject forged node events into a Stacks signer - (File: libsigner/src/events.rs)

### Summary
The `SignerEventReceiver::next_event` implementation, which handles inbound HTTP POSTs meant to come only from the paired Stacks node's event dispatcher, performs no authentication check whatsoever on the request before parsing and forwarding its body as a trusted `SignerEvent`.

### Finding Description
`SignerEventReceiver::bind` opens a plain `tiny_http::Server` on the configured listener address (per `docs/signing.md`, commonly `0.0.0.0:30000`, i.e. reachable from the network, not just loopback): [1](#0-0) 

`next_event` then dispatches based solely on the URL path and HTTP method — `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown` — with **no header, token, or peer-identity check** at any point in the path: [2](#0-1) 

The dispatched `process_event` function reads the raw body and directly JSON-deserializes it into the target event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`), then converts it into a `SignerEvent` and hands it to the signer runloop as if the trusted node had sent it: [3](#0-2) 

By contrast, `docs/signing.md` documents an `auth_token`/`auth_password` pairing intended to secure this node→signer channel: [4](#0-3) 

That auth value is used on the node's outbound event-dispatcher side (`stackslib/src/config` / event dispatcher, outside the reviewed scope), but nothing in `libsigner/src/events.rs` — the receiving side actually reachable by this transport — ever inspects an `Authorization` header or compares it to any configured secret. A `grep` across `stackslib/src/**` and `libsigner/**` for `auth_token`/`Authorization`/`check_auth` returns no matches, confirming the receiver-side check does not exist in this code path.

This breaks the intended equality "event originated from the paired Stacks node" vs. "event originated from any TCP client that can reach the bound port." The auth-gate documented as mandatory (`auth_password` must match `auth_token`) fails open on the receiver: it simply isn't checked.

Note: `StackerDBChunksEvent`'s individual chunks still carry `StackerDBChunkData` signatures that are verified elsewhere against on-chain signer sets, so *StackerDB* content itself has an independent authenticity layer. However, `BlockValidateResponse` (`/proposal_response`), `BurnBlockEvent` (`/new_burn_block`), and `StacksBlockEvent` (`/new_block`) carry no such independent chunk-level signature check at this ingestion point — they are trusted purely because they arrived on this HTTP listener.

### Impact Explanation
Any remote, unprivileged network peer that can reach the signer's bound event-receiver port can POST a forged `/proposal_response`, `/new_burn_block`, or `/new_block` JSON body and have it accepted and forwarded into the signer's runloop as a legitimate node-originated event. Depending on how the signer runloop consumes these events (e.g. treating a forged `BlockValidationResponse` as authoritative for signing decisions, or forged burn/tip notifications as chain-state truth), this is an unauthenticated write of attacker-controlled state into the signer process — i.e., forged data accepted and propagated into the signer's decision-making state, matching the "Critical: unauthenticated/unauthorized write to state" / "auth bypass" category.

### Likelihood Explanation
High for any deployment where the signer's event-receiver endpoint is bound to a non-loopback interface (the sample config in `docs/signing.md` shows `endpoint = "0.0.0.0:30000"`). No credentials, node key, or privileged role is required — only network reachability to the port and knowledge of the fixed, publicly-documented URL paths (`/proposal_response`, `/new_burn_block`, `/new_block`, `/stackerdb_chunks`).

### Recommendation
Add an authentication check in `SignerEventReceiver::next_event` (or in `process_event`) that validates a shared secret/token (e.g., an `Authorization` header compared against the configured `auth_password`) before parsing and forwarding any request body, rejecting unauthenticated requests with 401 rather than parsing them as trusted events.

### Proof of Concept
1. Start a `stacks-signer` with `endpoint = "0.0.0.0:30000"` per the sample config in `docs/signing.md`.
2. From any other host, send:
```
POST /new_burn_block HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"burn_height":999999,"burn_header_hash":"...","consensus_hash":"...","parent_burn_block_hash":"..."}
```
3. Observe that `process_event::<T, BurnBlockEvent>` in `libsigner/src/events.rs` (lines 519-542) deserializes and forwards this as a genuine `SignerEvent::NewBurnBlock` to the signer runloop, with no check that the request came from the paired node — confirmed by tracing `next_event` (lines 413-458), which contains no auth/token verification before dispatch.

### Citations

**File:** libsigner/src/events.rs (L401-409)
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

**File:** docs/signing.md (L26-48)
```markdown
[node]
stacker = true

[[events_observer]]
endpoint = "127.0.0.1:30000"
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
