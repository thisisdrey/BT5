### Title
Signer event-receiver HTTP endpoint accepts unauthenticated POSTs, allowing forged `BlockValidateResponse`/`BurnBlockEvent` injection — ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer`'s `SignerEventReceiver` binds an HTTP server (documented default `0.0.0.0:<port>`) that is supposed to only accept events from its paired `stacks-node`. The node/signer docs describe an `auth_token`/`auth_password` pair as the mechanism that authenticates this channel, but the receiver code never checks any credential on incoming requests, so any host that can reach the bound port can post forged events directly into the signer's run loop.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` dispatches incoming HTTP POSTs purely by URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown`) with no inspection of headers, tokens, or peer identity: [1](#0-0) 

`process_event` simply reads the body and JSON-deserializes it into the corresponding event type, again with no authentication step: [2](#0-1) 

The operator documentation explicitly claims this channel is protected by a shared secret: [3](#0-2) [4](#0-3) 

However, `auth_token`/`auth_password` is only wired up on the *node's* HTTP RPC surface — specifically the `/v3/block_proposal` handler that the signer calls into the node — not on the signer's own inbound listener: [5](#0-4) 

Grepping `stacks-signer/src/**` shows `auth_password`/`auth_token` only appear in `config.rs` (parsing the value) and `client/stacks_client.rs` (attaching it to *outgoing* requests to the node's RPC API) — never consulted by `SignerEventReceiver`. This means the equality the docs promise ("signer's incoming events are only accepted from an authenticated node") is never actually enforced by code: the check that should gate `/proposal_response`, `/new_burn_block`, and `/new_block` submissions is absent, so the "authenticated" and "actually verified" sets are not equal — any unauthenticated remote sender is treated identically to the paired node.

Note that `/stackerdb_chunks` payloads are partially mitigated because each `StackerDBChunkData` slot carries its own signature that downstream consumers may verify against the expected signer set, but `BlockValidateResponse` (`/proposal_response`) and `BurnBlockEvent` (`/new_burn_block`) carry no such signature and are consumed directly by the signer's state machine.

### Impact Explanation
This is a remote, unauthenticated write into the signer's internal state: an attacker who can reach the bound HTTP port (which per the sample configs binds to `0.0.0.0`) can inject forged `BlockValidateResponse` (fake block-validation "OK"/"error" results) or `BurnBlockEvent` messages, or resend arbitrary `StackerDBChunksEvent` payloads, directly into the `SignerRunLoop`. Depending on how these events are consumed by the signer's block-approval bookkeeping, this can corrupt the signer's view of proposal validation outcomes or burn-block arrivals — a request-smuggling/auth-bypass class issue on a security-critical control channel. Because this is out-of-band from the node's own authenticated RPC and StackerDB signature checks, it lets an unauthorized party feed data the signer trusts as if it came from its paired node.

### Likelihood Explanation
Likelihood depends entirely on network exposure of the signer's event-receiver port. Operators are told to bind this to a local/private endpoint, and many deployments may firewall it, but the code itself provides no defense-in-depth: nothing prevents any reachable client from POSTing to these paths, and the documented `auth_token`/`auth_password` gives operators a false sense that this channel is authenticated when it is not checked in `libsigner/src/events.rs`. Any misconfiguration (binding to a routable interface, container port exposure, etc.) makes this directly and trivially exploitable with a single crafted HTTP POST.

### Recommendation
Add an authentication check to `SignerEventReceiver::next_event`/`process_event` that validates a shared secret (the same `auth_token`/`auth_password` already documented) via an `Authorization` header or similar, rejecting any request that doesn't present the correct credential before deserializing the body into a `SignerEvent`. This should apply uniformly to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block`.

### Proof of Concept
1. Deploy a `stacks-signer` with default config binding `endpoint = "0.0.0.0:30000"`.
2. From any host that can reach port 30000 (no credentials needed), send:
   ```
   POST /proposal_response HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   { ... forged BlockValidateResponse JSON ... }
   ```
3. The `SignerEventReceiver` in `libsigner/src/events.rs` (`next_event` → `process_event::<T, BlockValidateResponse>`) accepts and deserializes this into a `SignerEvent`, forwarding it into the signer's run loop as if it originated from the paired, authenticated stacks-node — with no token/header verification performed anywhere in the path shown above.

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

**File:** docs/signing.md (L51-59)
```markdown
### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```

**File:** sample/conf/mainnet-signer.toml (L9-11)
```text
# Key coordination points between this config and the signer binary:
#   - [[events_observer]] endpoint must match signer's `endpoint`
#   - [connection_options] auth_token must match signer's `auth_password`
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1-1)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
```
