### Title
`SignerEventReceiver` accepts unauthenticated event POSTs, allowing forged StackerDB/block-validation/burn-block events to be injected into the signer - ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer` binary's event-receiving HTTP transport (`SignerEventReceiver::next_event`, in `libsigner/src/events.rs`) accepts and processes `POST /stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` requests from any TCP client without verifying any credential, token, or origin. Configuration guidance (`docs/signing.md`, `sample/conf/signer/mainnet-signer-conf.toml`) explicitly instructs operators to bind this endpoint to `0.0.0.0:30000` (all interfaces), meaning any network-reachable attacker can submit forged events that are indistinguishable from events legitimately sent by the trusted `stacks-node`.

### Finding Description
`SignerEventReceiver::next_event` dispatches based solely on URL path: [1](#0-0) 

There is no check of an `Authorization` header, shared secret, or peer allow-list anywhere in this file — a full-repo search for `Authorization`/`auth_token` inside `libsigner/**` returns zero matches, confirming the transport is entirely unauthenticated. `process_event` simply reads the body and deserializes it into the corresponding event type: [2](#0-1) 

By contrast, the equivalent trust boundary running in the opposite direction (signer → node, e.g. for block-proposal RPC) is explicitly protected by an `auth_token`/`Authorization` header mechanism documented in the node config: [3](#0-2) 

and the signer-side config documentation stresses that `auth_password` "must match" this node-side token: [4](#0-3) 

However, this `auth_token`/`auth_password` pairing is used to authenticate the *node's* HTTP RPC endpoints (e.g., `/v3/block_proposal`, `/v2/blocks?broadcast=1`) — it is never consulted by `SignerEventReceiver` when the *signer* accepts inbound event POSTs. The equality that should hold — "chunk/event received by the signer's listener" == "chunk/event legitimately emitted by our configured stacks-node" — is broken: any TCP peer that can reach the bound listener can satisfy this equality by simply crafting a well-formed JSON body, since no signer-side authentication gate exists on this path at all.

The reference configuration explicitly recommends binding this listener to all interfaces: [5](#0-4) 

This is analogous to the reported Vepoch.sol issue in that a feature intended to be reachable only by one trusted, authorized party (the node forwarding rewards/events) instead accepts input from *any* caller because the authorization/equality check that should gate the action is missing — "authenticated vs. actually accepted" breaks down exactly as "authorized redirection recipient vs. actual attacker-controlled recipient" did in the original report.

### Impact Explanation
An attacker who can reach the signer's listening port can:
- POST a forged `/stackerdb_chunks` body, injecting a `StackerDBChunksEvent` that is fed straight into the signer's runloop as if the node had observed it — this is an unauthenticated write into signer-observed state (`StackerDBChunkData` signature checks happen only later inside signer business logic when consuming the message content, not at the transport layer).
- POST a forged `/proposal_response` (`BlockValidateResponse`) or `/new_burn_block`/`/new_block` body to inject a fabricated validation result or burn/stacks-tip notification, which the signer treats as though the trusted node produced it.

This satisfies the "Critical" bar of "unauthenticated/unauthorized write to state" via the network-facing transport in `libsigner`, since the injected data enters the signer's processing pipeline (`forward_event` → signer runloop) without any credential check.

### Likelihood Explanation
High, given operator guidance recommends binding to `0.0.0.0`, and no code path in `libsigner/src/events.rs` performs any authentication regardless of bind address — a single crafted HTTP POST suffices. The only mitigating factor is that some deployments may bind to a loopback address or firewall the port, but this is an operational choice, not an enforced control in the code.

### Recommendation
Require and verify a shared secret / HMAC / auth token on all inbound requests handled by `SignerEventReceiver::next_event` (mirroring the `auth_token` mechanism already used for the node's HTTP RPC surface), rejecting any `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` request that lacks a valid credential, before further JSON deserialization or forwarding to the runloop.

### Proof of Concept
1. Deploy a `stacks-signer` with the documented reference config (`endpoint = "0.0.0.0:30000"`).
2. From a remote host, send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{ ... forged BlockValidateResponse JSON ... }
```
3. Observe (per `libsigner/src/events.rs` lines 437-458) that the request is dispatched to `process_event::<T, BlockValidateResponse>` and, if the JSON deserializes successfully, forwarded via `forward_event` into the signer runloop — with no authentication check performed anywhere in the path.

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

**File:** stackslib/src/config/mod.rs (L3799-3816)
```rust
    /// ---
    /// @default: `false`
    pub private_neighbors: Option<bool>,
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

**File:** docs/signing.md (L37-59)
```markdown
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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-50)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"

# REQUIRED: Network selection.
# Valid values: "mainnet", "testnet", "mocknet"
network = "mainnet"

# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
