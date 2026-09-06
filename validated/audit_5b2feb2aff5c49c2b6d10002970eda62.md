### Title
Signer's event-receiver HTTP listener accepts unauthenticated POSTs, allowing forged StackerDB/block/burn-block events to be injected into the signer runloop - ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer` binary runs an HTTP server (`SignerEventReceiver`) that the `stacks-node` is supposed to push trusted event notifications to (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`). Operator documentation and sample configs describe an `auth_token`/`auth_password` shared secret that is meant to bind the node and the signer together, but the signer-side listener never checks any authentication header before accepting and forwarding these events into the signer's decision runloop.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` reads an incoming HTTP POST, dispatches purely based on the URL path, and hands the body straight to `process_event`: [1](#0-0) 

`process_event` deserializes the JSON body into the target event type and converts it into a `SignerEvent`, with no check of any request header, token, or peer identity: [2](#0-1) 

The event is then unconditionally forwarded to the signer's runloop channel via `forward_event`: [3](#0-2) 

A full grep of `libsigner/**` for any `Authorization`/`auth_token`/`bearer` handling returns zero matches — the transport layer that is supposed to be paired with the node via a shared secret performs no authentication whatsoever.

Meanwhile, the documented deployment model explicitly frames `auth_token` (node side) / `auth_password` (signer side) as a security boundary that "must match" between node and signer: [4](#0-3) [5](#0-4) 

And the sample/mainnet signer binds its event-receiver socket on `0.0.0.0:30000`/configurable `endpoint`, i.e. potentially reachable from any interface, not just loopback: [6](#0-5) 

This "must match" secret, however, is consumed elsewhere (authenticating signer→node RPC calls such as write endpoints protected by `auth_token` in `stackslib/src/net/httpcore.rs` / `postblock_proposal.rs`), not by the signer's own inbound listener. The equality that fails is: the operator's expectation ("auth_token/auth_password gates node↔signer traffic") vs. the actual code ("the signer's HTTP receiver accepts any POST to a known path from any source"). This is the classic *auth-gate that fails open* pattern from the referenced Substrate fix's bug class — a control that is documented/intended to authenticate a message channel simply isn't wired into the code path that receives it.

### Impact Explanation
Any remote, unprivileged party that can reach the signer's bound event-receiver port can POST fabricated `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` payloads. These are fed directly into the signer's `run_one_pass` logic as if they legitimately originated from the paired stacks-node. Depending on which event type is forged, this can inject spoofed StackerDB chunk observations, fake block-validation results, or fake burn-block observations into the signer's internal state machine — an unauthenticated write into a component that ultimately participates in block signing decisions. This is a Critical-class issue per the ruleset ("unauthenticated/unauthorized write to state ... auth bypass") since it defeats the one credential (`auth_token`/`auth_password`) that operator docs claim protects this channel.

### Likelihood Explanation
Likelihood is high wherever the signer's endpoint is reachable beyond localhost (the shipped sample configs explicitly show `0.0.0.0` binding), and even on loopback-only deployments, any local process or container-network peer can reach it. No cryptographic material, node secret, or privileged position is required — only network reachability to the configured `endpoint` port, and knowledge of the fixed set of documented URL paths (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), which are public documentation (`docs/event-dispatcher.md`).

### Recommendation
Add authentication to `SignerEventReceiver`'s HTTP handling (e.g., verify a bearer/`Authorization` header against the configured `auth_password` before dispatching to `process_event`), matching the trust model already documented for `auth_token`/`auth_password`. Reject requests without a valid token with an HTTP 401/403 rather than processing them.

### Proof of Concept
1. Deploy a signer per `sample/conf/signer/mainnet-signer-conf.toml`, with `endpoint = "0.0.0.0:30000"` and `auth_password` set.
2. From a separate, unprivileged host (or a local process not otherwise authorized), send a raw HTTP POST directly to the bound port:
   ```
   POST /stackerdb_chunks HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <len>

   {"contract_id": "...", "modified_slots": [ ... attacker-crafted chunk ... ]}
   ```
   No `Authorization` header or `auth_password` value is supplied.
3. Observe (as shown by the existing test harness pattern in `libsigner/src/tests/mod.rs::test_simple_signer`, which performs an equivalent unauthenticated POST) that the event receiver accepts the request, deserializes it, and forwards it into the signer runloop exactly as though it came from the trusted node — confirming there is no authentication gate on this path. [7](#0-6) 

**Uncertainty note:** I could not verify within the available context whether `stacks-signer`'s higher-level runloop (`stacks-signer/src/runloop.rs`, out of scope per the rules as "signer decision logic") performs any secondary authentication/sanity check that might mitigate impact of forged events before they influence signing behavior. The finding above is scoped strictly to the transport-layer (`libsigner`) lack of authentication, which is in-scope per the rules.

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

**File:** libsigner/src/events.rs (L517-542)
```rust
// TODO: add tests from mutation testing results #4835
#[cfg_attr(test, mutants::skip)]
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

**File:** docs/follower.md (L61-70)
```markdown
[node]
stacker = true

[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```
```

**File:** sample/conf/mainnet-signer.toml (L9-38)
```text
# Key coordination points between this config and the signer binary:
#   - [[events_observer]] endpoint must match signer's `endpoint`
#   - [connection_options] auth_token must match signer's `auth_password`

[node]
# working_dir = "/dir/to/save/chainstate" # defaults to: /tmp/stacks-node-[0-9]*
rpc_bind = "0.0.0.0:20443"
p2p_bind = "0.0.0.0:20444"
prometheus_bind = "0.0.0.0:9153"
stacker = true

[burnchain]
mode = "mainnet"
peer_host = "127.0.0.1"

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

**File:** docs/signing.md (L37-49)
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
```

**File:** libsigner/src/tests/mod.rs (L120-147)
```rust
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
    });
```
