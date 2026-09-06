### Title
Unauthenticated HTTP event-ingestion endpoint in `SignerEventReceiver` accepts forged node events despite a documented `auth_password` protection — (File: `libsigner/src/events.rs`)

### Summary
The stacks-signer's HTTP event receiver (`SignerEventReceiver::next_event`, `libsigner/src/events.rs`) accepts and processes `POST` requests to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` from any TCP peer that can reach the bound socket, with no authentication check whatsoever on the incoming request. The signer's own documentation (`docs/signing.md`) describes an `auth_password` setting on the signer side that is supposed to be matched against the node's `[connection_options] auth_token`, implying that only a node presenting the correct token should be able to push events. In the transport code, this mechanism is never implemented — there is no header check, no token comparison, nothing gating `process_event()`. This mirrors the reported bug class exactly: a documented authentication knob that is parsed/described but never wired into the actual request-handling path, so the server runs open regardless of configuration.

### Finding Description
`SignerEventReceiver<T>::next_event` (`libsigner/src/events.rs:413-459`) reads an incoming HTTP request from the internal `tiny_http`-based server and dispatches purely on `request.url()`: [1](#0-0) 

For every recognized path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), the request body is handed directly to `process_event`, which does nothing but deserialize JSON into the target event type and `ack`s the sender: [2](#0-1) 

At no point is any `Authorization` header, bearer token, or shared secret checked against a configured value. A `grep` across the entire `libsigner` crate for `auth_token`, `auth_password`, or `Authorization` returns zero matches — the check simply does not exist anywhere in the transport layer.

Yet the project's own documentation (`docs/signing.md`) explicitly describes this as a security boundary: [3](#0-2) 

It instructs operators to set `auth_password` on the signer and match it to `auth_token` in the node's `[connection_options]`, describing this as something that "must match" between the two components — i.e., presented to the operator as an authentication mechanism. In reality, `SignerEventReceiver` never reads or enforces any such value on the inbound HTTP path, so the "protection" is entirely cosmetic, exactly as in the reported `praisonai serve --api-key` case where the flag was parsed but never checked.

The event types accepted here are trusted inputs to the signer's decision-making pipeline: `StackerDBChunksEvent` (chunk data forwarded from the node, which is separately protected by StackerDB slot signatures via `StackerDBChunkData::verify`), but `BlockValidateResponse` (`/proposal_response`) and `BurnBlockEvent` (`/new_burn_block`) and `StacksBlockEvent` (`/new_block`) carry no independent signature of their own at this transport layer — they rely entirely on the assumption that only the configured node can reach this port and POST to it. Since that assumption is not enforced by any code, any host that can reach the signer's bound address (which operators may expose beyond loopback, e.g. in containerized/multi-host signer deployments where the node and signer run on separate machines, as the sample configs' `endpoint = "0.0.0.0:30000"` suggest) can forge these events and inject them straight into the signer's `SignerRunLoop`.

### Impact Explanation
An attacker who can reach the signer's event-receiver port can inject forged `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` messages without any credential, causing the signer to act on attacker-controlled data as if it originated from the trusted node it is configured to listen to. This is an unauthenticated write into a component that directly feeds the block-signing decision pipeline — a documented auth control (`auth_password`/`auth_token`) fails open because it was never implemented in the transport code. Per the scoring rubric this constitutes an auth bypass into the signer input path, reachable purely by network access to the port the operator believed was protected by the documented password mechanism.

### Likelihood Explanation
Exploitation requires only TCP reachability to the signer's configured `endpoint` and knowledge of the fixed, small set of URL paths (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), all of which are documented in `docs/signing.md` and the source itself. No secret material, node key, or privileged role is needed — this is precisely the "remote, unprivileged" class required. The likelihood of exposure depends on deployment topology (loopback-only vs. routed network), but the vulnerability is a code-level omission independent of deployment, and the project's own sample configs and docs describe non-loopback binding as a supported configuration.

### Recommendation
Implement the documented `auth_password`/`auth_token` check in `SignerEventReceiver::next_event` (or in `process_event`) before deserializing/dispatching any event: require and validate an `Authorization` header against the configured secret using a constant-time comparison, and reject unauthenticated requests with `401 Unauthorized` (mirroring the pattern already correctly used in `stackslib/src/net/api/postblock_v3.rs` and `postblock_proposal.rs`). Fail closed by default when no password is configured and the receiver is bound to a non-loopback address, or at minimum emit a startup warning.

### Proof of Concept
```rust
// Minimal PoC sketch demonstrating the missing auth check in
// libsigner/src/events.rs::next_event / process_event.
//
// 1. Start a SignerEventReceiver bound to e.g. 0.0.0.0:30000 (as shown in
//    libsigner/src/tests/mod.rs::test_simple_signer / test_status_endpoint,
//    which set up the exact same receiver without any auth configuration).
// 2. From an unauthenticated client, POST a forged event body directly:

let body = serde_json::to_string(&forged_burn_block_event).unwrap();
let req = format!(
    "POST /new_burn_block HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\
     Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
    body.len(), body
);
let mut sock = TcpStream::connect(addr).unwrap();
sock.write_all(req.as_bytes()).unwrap();
// No Authorization header sent, no auth_password configured or checked
// anywhere in libsigner/src/events.rs — the forged event is accepted,
// deserialized, and forwarded into the signer runloop.
```
This is directly modeled on the existing `test_simple_signer`/`test_status_endpoint` tests (`libsigner/src/tests/mod.rs:89-148, 199-230`), which already demonstrate that any TCP client speaking raw HTTP can push events into the receiver with zero credentials, confirming the absence of the documented authentication gate.

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

**File:** libsigner/src/events.rs (L519-541)
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
