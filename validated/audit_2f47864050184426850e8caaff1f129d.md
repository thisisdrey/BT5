Confirmed: `stacks-node/src/event_dispatcher.rs` has no `auth_token`/`Authorization` header logic at all — it sends event-observer payloads (including `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`) unauthenticated. The `auth_token` field in `[connection_options]` is used only by `stackslib/src/net/api/postblock_proposal.rs` to authenticate the *node's* RPC endpoint (miner/other party → node direction), not the node → signer event-push direction. On the signer's receiving side, `SignerEventReceiver::next_event()` in `libsigner/src/events.rs` dispatches purely on URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown`) with **no credential check whatsoever**.

### Title
Signer's event-listener HTTP endpoint accepts forged node events with no authentication — ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer` binary runs an HTTP server (`SignerEventReceiver`) that is supposed to receive `stackerdb_chunks`, `proposal_response`, and `new_burn_block` events only from its paired `stacks-node`. Despite documentation and sample configs (`docs/signing.md`, `sample/conf/mainnet-signer.toml`, `sample/conf/testnet-signer.toml`) explicitly stating that the node's `[connection_options] auth_token` must match the signer's `auth_password` "for coordination," no such token is ever checked (or even read) by `SignerEventReceiver::next_event()`, and the node's `EventDispatcher` never attaches any `Authorization` header when POSTing events. Any host that can reach the signer's listening socket can POST directly to `/stackerdb_chunks`, `/proposal_response`, or `/new_burn_block` and have the payload accepted as if it came from the trusted node.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` (lines 413–459) dispatches solely on the request path:
```
if request.url() == "/stackerdb_chunks" {
    process_event::<T, StackerDBChunksEvent>(request)
} else if request.url() == "/proposal_response" {
    process_event::<T, BlockValidateResponse>(request)
} else if request.url() == "/new_burn_block" {
    process_event::<T, BurnBlockEvent>(request)
} ...
```
There is no header/token check anywhere in this file, in `process_event` (lines 519–542), or in `libsigner/src/libsigner.rs`. `stacks-node/src/event_dispatcher.rs`'s `dispatch_to_observer_or_log_error`/`send_stackerdb_chunks` functions likewise contain no `auth_token`/`Authorization` logic — confirmed by grep showing zero matches for `auth_token`/`Authorization` in that file. The only place `auth_token` is actually enforced is `stackslib/src/net/api/postblock_proposal.rs`, which authenticates requests *into* the node's RPC (e.g., block-proposal submissions), not the node's outbound event pushes to the signer.

This breaks the intended equality "event accepted by signer == event actually originated from the paired, trusted stacks-node." Anyone with network access to the signer's bound endpoint (which per the sample configs may be bound to `0.0.0.0:30000`) can forge a `StackerDBChunksEvent`, `BlockValidateResponse`, or `BurnBlockEvent` and have it processed exactly like a genuine node-originated event, entering the signer's runloop via `forward_event`.

### Impact Explanation
This is a High/Critical-impact authentication bypass on a signer-facing control-plane channel: an unauthenticated party can inject forged `StackerDBChunksEvent` payloads (subsequently parsed as miner/signer messages, see `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` in the same file) or `BlockValidateResponse`/burn-block events directly into the signer's decision pipeline, i.e., unauthenticated write into signer-facing state that downstream signer logic treats as coming from the local trusted node. Depending on deployment (endpoint bound to a non-loopback address, or reachable from a compromised adjacent host), this allows spoofed data to reach the signer without ever touching real StackerDB signature checks that gate P2P/RPC-sourced chunks (`try_replace_chunk`, `validate_received_chunk` in `stackslib/src/net/stackerdb/`).

### Likelihood Explanation
Likelihood depends on operational exposure: the sample configs bind the signer's `endpoint` to `127.0.0.1:30000` by default (`sample/conf/mainnet-signer.toml`, `docs/signing.md`), which mitigates remote exploitation when properly localhost-bound. However, the signer's own sample config in `docs/signing.md` shows `endpoint = "0.0.0.0:30000"`, and nothing in the code enforces loopback-only binding or authentication as a safety net if an operator (or a compromised co-located process/container) can reach that port. Given the documentation strongly implies `auth_token`/`auth_password` are meant to be the actual security boundary for this channel, and no code path enforces it, this is a real gap rather than a defense-in-depth nicety.

### Recommendation
Implement and enforce the documented `auth_token`/`auth_password` shared secret: have `stacks-node`'s `EventDispatcher::dispatch_to_observer_or_log_error` (and callers of `send_stackerdb_chunks`/`send_proposal_response`/`send_new_burn_block`) attach an `Authorization` header populated from `connection_options.auth_token`, and have `SignerEventReceiver::next_event` in `libsigner/src/events.rs` validate that header against the signer's configured `auth_password` before calling `process_event`, rejecting (401) any request that fails the check.

### Proof of Concept
1. Stand up a `stacks-signer` per `sample/conf/signer/mainnet-signer-conf.toml`, with `endpoint` reachable (e.g., bound non-loopback, or from a co-tenant on the same host/network).
2. From any host with network access to that endpoint (no knowledge of `auth_password`/`auth_token` required), send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-endpoint>
Content-Type: application/json
Content-Length: <n>

{"contract_id": "...", "modified_slots": [ ... forged chunk with attacker-controlled sig ... ]}
```
mirroring the test harness pattern in `libsigner/src/tests/mod.rs` lines 121–146 (`mock_stacks_node` thread), which demonstrates the exact wire format accepted with zero authentication.
3. Observe the payload is parsed by `process_event::<T, StackerDBChunksEvent>` and forwarded into the signer runloop via `forward_event`, indistinguishable from a legitimate node-sourced event — no `auth_token` check is performed at any point in this path. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** libsigner/src/events.rs (L413-459)
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

**File:** docs/signing.md (L33-59)
```markdown
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

**File:** sample/conf/mainnet-signer.toml (L24-38)
```text
# Signer event observer (REQUIRED).
# WARNING: endpoint must match your signer binary's `endpoint` config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

# Optional: API event observer for stacks-blockchain-api service
# [[events_observer]]
# endpoint = "localhost:3700"
# events_keys = ["*"]
# timeout_ms = 60_000

[connection_options]
# WARNING: Must match the signer binary's `auth_password`.
auth_token = ""
```

**File:** stacks-node/src/event_dispatcher.rs (L1294-1300)
```rust
    fn send_stackerdb_chunks(&self, event_observer: &EventObserver, payload: &serde_json::Value) {
        self.dispatch_to_observer_or_log_error(event_observer, payload, PATH_STACKERDB_CHUNKS);
    }

    fn send_new_burn_block(&self, event_observer: &EventObserver, payload: &serde_json::Value) {
        self.dispatch_to_observer_or_log_error(event_observer, payload, PATH_BURN_BLOCK_SUBMIT);
    }
```

**File:** libsigner/src/tests/mod.rs (L118-146)
```rust
    let thread_chunks = chunks.clone();

    // simulate a node that's trying to push data
    let mock_stacks_node = thread::spawn(move || {
        let mut num_sent = 0;
        while num_sent < thread_chunks.len() {
            let mut sock = match TcpStream::connect(endpoint) {
                Ok(sock) => sock,
                Err(..) => {
                    sleep_ms(100);
                    continue;
                }
            };

            let ev = &thread_chunks[num_sent];
            let body = serde_json::to_string(ev).unwrap();
            let req = format!(
                "POST /stackerdb_chunks HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                endpoint,
                body.len(),
                body
            );
            debug!("Send:\n{}", &req);

            sock.write_all(req.as_bytes()).unwrap();
            sock.flush().unwrap();

            num_sent += 1;
        }
```
