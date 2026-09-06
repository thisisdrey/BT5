### Title
Unauthenticated event-injection into `stacks-signer`'s HTTP event receiver - (File: libsigner/src/events.rs)

### Summary
The reported GHSA-j562-c3cw-3p5g bug class is "a proxy component forwards a sensitive credential/data to a backend without a way to gate who receives/injects it." The closest remote, unprivileged analog in the in-scope transport code is that `SignerEventReceiver` (the `libsigner` HTTP transport that a `stacks-signer` binds to receive events pushed from a `stacks-node`) performs **no authentication whatsoever** on any of the messages it accepts, breaking the implicit equality "message came from the paired, trusted node" vs. "message came from anyone who can reach the listening socket."

### Finding Description
`SignerEventReceiver::next_event` dispatches incoming HTTP POSTs purely by URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/shutdown`, `/status`) with no header, token, or peer-identity check anywhere in the request path: [1](#0-0) 

The body is simply JSON-deserialized into a `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` and handed to the signer runloop: [2](#0-1) 

Even `/shutdown` sets the stop signal and terminates the receiver with no authentication at all: [3](#0-2) 

Compare this to the equivalent node-side endpoints, which do gate similarly sensitive write paths with an `authorization` header equal to a configured secret (`RPCPostBlockRequestHandler`, `RPCBlockProposalRequestHandler`, `RPCNakamotoBlockReplayRequestHandler`, `RPCTransactionSimulateRequestHandler` all check `preamble.headers.get("authorization")` against `self.auth`): [4](#0-3) [5](#0-4) 

The node's own client code (`stacks-signer`'s `StacksClient`) does send an `AUTHORIZATION` header when it *talks to the node* (`post_block`, `submit_block_for_validation`), confirming that this auth mechanism exists as a design pattern in this codebase: [6](#0-5) [7](#0-6) 

But the *reverse* direction — the node pushing events into the signer's `SignerEventReceiver` — has no analogous check. The `auth_token`/`auth_password` documented in `docs/signing.md` only secures the node's `/v3/block_proposal` inbound HTTP endpoint and is never referenced by, or passed to, `SignerEventReceiver`: [8](#0-7) [9](#0-8) 

Since the signer's event listener endpoint is a plain TCP/HTTP bind address (`endpoint = "0.0.0.0:30000"` per the sample docs) with no credential check, any host that can route to that port can forge `StackerDBChunksEvent`s (containing arbitrary miner/signer chunk data, still gated by chunk signature verification inside the signer's message parsing) or `BlockValidateResponse`/`BurnBlockEvent` payloads (which are *not* independently re-verified against chain state before being forwarded into the signer runloop) and inject them as if they came from the trusted node.

### Impact Explanation
This breaks the equality "events consumed by the signer runloop == events legitimately emitted by the paired node," which is the trust boundary the whole signer/node pairing depends on. Depending on which endpoint is abused:
- `/proposal_response` and `/new_burn_block` payloads are deserialized and forwarded to the signer state machine without any cross-check that they originated from the real node's chain state, which could desynchronize or manipulate a signer's view of block-validation results or burnchain state — a form of "unauthorized write to state."
- `/shutdown` gives an unauthenticated remote party a trivial DoS against the signer's event loop.

This is a High/Critical-class issue per the impact taxonomy given (unauthenticated write to state / unauthenticated DoS from a few messages), though the actual downstream effect depends on the signer runloop logic that consumes these events, which is explicitly out of scope for deeper analysis here.

### Likelihood Explanation
Exploitability is entirely gated by network reachability of the signer's event-listener port. In a well-operated deployment this port should be bound to localhost/private interfaces reachable only by the paired node, in which case this is not remotely exploitable by an unprivileged third party. However, the code itself enforces no such restriction — the bind address is fully operator-configurable, and the transport layer (`libsigner/src/events.rs`) provides zero authentication as a defense-in-depth measure, unlike the equivalent node-side endpoints that do implement a shared-secret check. I could not fully verify within the available context whether any other layer (e.g., firewall guidance, systemd hardening, or reverse-proxy requirement) is documented as a mandatory mitigation, or whether newer code paths add a token check I did not locate — this should be confirmed with a full-repo read before treating this as an urgent fix.

### Recommendation
Add an optional shared-secret (or the existing `auth_token`) check to `SignerEventReceiver`'s HTTP dispatch (e.g., require a matching `Authorization` header on `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/shutdown`), mirroring the pattern already used in `stackslib/src/net/api/postblock_v3.rs` and `blockreplay.rs`, and document that the listener should be bound to a private/loopback interface by default.

### Proof of Concept
1. Stand up a `stacks-signer` with `endpoint = "0.0.0.0:30000"` per the sample config.
2. From a separate, unprivileged host with network access to port 30000, send:
   ```
   POST /new_burn_block HTTP/1.1
   Host: <signer-host>:30000
   Content-Type: application/json
   Content-Length: <n>

   {"burn_block_hash": "...", "burn_block_height": ..., "consensus_hash": "...", "parent_burn_block_hash": "..."}
   ```
   with fabricated field values.
3. Because `next_event` (`libsigner/src/events.rs:413-458`) performs no authentication, the request is accepted, deserialized, and forwarded to the signer runloop as `SignerEvent::NewBurnBlock`, exactly as if it had been sent by the legitimate paired node.

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

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blockreplay.rs (L574-583)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stacks-signer/src/client/stacks_client.rs (L296-308)
```rust
        let timer = crate::monitoring::actions::new_rpc_call_timer(
            &self.block_proposal_path(),
            &self.http_origin,
        );
        let send_request = || {
            self.stacks_node_client
                .post(self.block_proposal_path())
                .header("Content-Type", "application/json")
                .header(AUTHORIZATION, self.auth_password.clone())
                .json(&block_proposal)
                .send()
                .map_err(backoff::Error::transient)
        };
```

**File:** stacks-signer/src/client/stacks_client.rs (L618-631)
```rust
        let path = format!("{}{}?broadcast=1", self.http_origin, postblock_v3::PATH);
        let timer = crate::monitoring::actions::new_rpc_call_timer(&path, &self.http_origin);
        let send_request = || {
            let response = self
                .stacks_node_client
                .post(&path)
                .header("Content-Type", "application/octet-stream")
                .header(AUTHORIZATION, self.auth_password.clone())
                .body(block.serialize_to_vec())
                .send()
                .map_err(|e| {
                    debug!("Failed to submit block to the Stacks node: {e:?}");
                    backoff::Error::transient(ClientError::from(e))
                })?;
```

**File:** docs/signing.md (L42-58)
```markdown
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
```

**File:** stackslib/src/config/mod.rs (L3802-3816)
```rust
    /// HTTP auth password to use when communicating with stacks-signer binary.
    ///
    /// This token is used in the `Authorization` header for certain requests.
    /// Primarily, it secures the communication channel between this node and a
    /// connected `stacks-signer` instance.
    ///
    /// It is also used to authenticate requests to `/v2/blocks?broadcast=1`.
    /// ---
    /// @default: `None` (authentication disabled for relevant endpoints)
    /// @notes:
    ///   - This field **must** be configured if the node needs to receive
    ///     block proposals from a configured `stacks-signer` [[events_observer]]
    ///     via the `/v3/block_proposal` endpoint.
    ///   - The value must match the token configured on the signer.
    pub auth_token: Option<String>,
```
