### Title
Unauthenticated event injection into signer's `SignerEventReceiver` HTTP listener — ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` (`libsigner/src/events.rs`) accepts and processes `POST /stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/shutdown` requests from any TCP client that can reach its bound socket, with **no authentication or origin check** of any kind. This is the same class of flaw as the reported bug: the code implicitly trusts "whoever is talking to me on this channel" as if it were the legitimate, singular counterparty (the Stacks node), exactly as `g8keepBondingCurveFactory` implicitly trusted `msg.sender` to be the intended human deployer. Here, any process/party capable of connecting to the listener socket can impersonate the node and inject state into the signer's runloop, including a full remote shutdown of the signer.

### Finding Description
`SignerEventReceiver` binds an HTTP server (`bind`) and its `next_event` loop dispatches based solely on the URL path, with no check of any shared secret, token, or peer identity: [1](#0-0) 

Notably the `/shutdown` path immediately sets `stop_signal` with no authentication at all: [2](#0-1) 

and `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` are deserialized and handed to `process_event`, which only checks that the JSON body deserializes into the expected type — it does not verify that the request originated from the actual Stacks node instance the signer is configured to trust: [3](#0-2) 

The resulting `SignerEvent` is forwarded straight into the signer's runloop via `forward_event`, which has no additional gate: [4](#0-3) 

By contrast, the sample node config documents an `auth_token` under `[connection_options]` that is meant to authenticate the *node-to-signer* event channel ("WARNING: Must match the signer's auth_password"): [5](#0-4) 

Searching the actual `libsigner` event-receiver code and the node's event-dispatcher path (`stackslib/src/event_dispatcher.rs`) for any use of `auth_token`/`Authorization` returned no matches — i.e., I could not find where this token is actually verified on the receiving (`libsigner`) side, or enforced when the node posts events. This mirrors the underlying bug class in the report: an entity (`DEPLOYER`/here, "trusted event source") is inferred from the immediate caller of an interface rather than being cryptographically verified, so any intermediary/attacker reaching that interface is treated as the legitimate party.

Individual `StackerDBChunkData` payloads inside a `StackerDBChunksEvent` do still carry their own `sig` field that is separately verifiable against the known slot signer (`StackerDBChunkData::verify`), so forged StackerDB chunk *content* would still fail downstream validation by a careful consumer. However:
- `BlockValidateResponse`, `BurnBlockEvent`, and `StacksBlockEvent` payloads are not itself protected by any signature at this layer, so forged instances of these events can be injected wholesale and consumed by the signer runloop as if they came from the node.
- The `/shutdown` endpoint provides a trivial unauthenticated remote DoS: any TCP peer that can reach the bound address can immediately terminate the signer's event receiver.

### Impact Explanation
This falls in the "Critical — remote crash/unauthenticated DoS from few messages" and potentially "unauthenticated ... write to state" bands defined by the rubric: an unauthenticated client can (a) trivially crash/stop the signer's event pipeline via `/shutdown`, and (b) inject forged `BlockValidateResponse` / `BurnBlockEvent` / `StacksBlockEvent` messages that the signer runloop will process as genuine node-originated state, potentially corrupting the signer's view of chain/burnchain/proposal state and its downstream signing decisions (though the signer's actual vote logic itself is out of scope per the rules, the *injection point* that feeds it false data is squarely in scope).

### Likelihood Explanation
Likelihood depends heavily on network exposure of the signer's event-listener bind address (`bind_addr`), which is operator-configured (commonly `127.0.0.1:<port>` per sample configs) — if bound to loopback only, exploitation requires local access. However, nothing in the reviewed `libsigner` code enforces this restriction or an authentication token at the transport level; the security boundary is left entirely to deployment configuration, and the documented `auth_token` mechanism referenced in node config samples does not appear to be checked in the `libsigner` receiver code that was inspected. Given the uncertainty around whether/where `auth_token` verification is actually wired in (I was unable to locate it in either `libsigner` or `stackslib/src/event_dispatcher.rs` in this pass), this should be treated as **Medium** confidence/likelihood pending confirmation of that wiring — it is possible auth is enforced elsewhere (e.g., in the HTTP layer used for outbound POSTs from the node) that I did not locate with the available search tools.

### Recommendation
- Require and verify a shared-secret/token (e.g., the `auth_token` referenced in configs) on every inbound request to `SignerEventReceiver`, rejecting requests without a valid `Authorization`/equivalent header before dispatch, including for `/shutdown`.
- Bind the event receiver to loopback by default and document/enforce that it must never be exposed to an untrusted network without an authenticated reverse proxy.
- Consider requiring `BlockValidateResponse`, `BurnBlockEvent`, and `StacksBlockEvent` payloads to carry a verifiable signature from the node's key, similar to how `StackerDBChunkData` is independently signed, so that the transport-level trust is not the sole security boundary.

### Proof of Concept
1. Start a signer with `SignerEventReceiver::bind` listening on `HOST:PORT` (as done in `libsigner/src/tests/mod.rs`'s `test_simple_signer`/`test_status_endpoint`).
2. From any process able to reach `HOST:PORT` (no credentials required), send:
   - `POST /shutdown` with any body → the receiver's `stop_signal` is set and the event loop terminates, as shown by the exact mechanism used by the legitimate `SignerStopSignaler::send` (`libsigner/src/events.rs:376-396`), which itself works precisely because no auth is required.
   - `POST /new_burn_block` (or `/new_block`, `/proposal_response`) with an attacker-crafted JSON body matching `BurnBlockEvent`/`StacksBlockEvent`/`BlockValidateResponse`'s `Deserialize` shape → `process_event` accepts it and `forward_event` delivers it into the signer runloop indistinguishably from a genuine node-originated event. [6](#0-5)

### Citations

**File:** libsigner/src/events.rs (L410-458)
```rust
    /// Wait for the node to post something, and then return it.
    /// Errors are recoverable -- the caller should call this method again even if it returns an
    /// error.
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

**File:** sample/conf/testnet-miner-conf.toml (L73-87)
```text
# ============================================================
# [connection_options] - Authentication for signer communication
# ============================================================
[connection_options]
# WARNING: Must match the signer's auth_password.
auth_token = "<YOUR_AUTH_TOKEN>"

# ============================================================
# [[events_observer]] - Signer event subscription
# ============================================================

# WARNING: endpoint must match your signer's endpoint config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```

**File:** libsigner/src/tests/mod.rs (L93-147)
```rust
#[test]
fn test_simple_signer() {
    // Use a `BlockPreCommit` payload on its matching contract (signers-0-3).
    let contract_id = NakamotoSigners::make_signers_db_contract_id(0, 3, false);
    let ev = SignerEventReceiver::new(false);
    let (res_send, _res_recv) = channel();
    let max_events = 5;
    let mut signer = Signer::new(SimpleRunLoop::new(max_events), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:30000".parse().unwrap();
    let mut chunks = vec![];
    for i in 0..max_events {
        let privk = Secp256k1PrivateKey::random();
        let message =
            SignerMessage::BlockPreCommit(Sha512Trunc256Sum([(i as u8).wrapping_add(1); 32]));
        let message_bytes = message.serialize_to_vec();
        let mut chunk = StackerDBChunkData::new(i as u32, 1, message_bytes);
        chunk.sign(&privk).unwrap();

        let chunk_event = StackerDBChunksEvent {
            contract_id: contract_id.clone(),
            modified_slots: vec![chunk],
        };
        chunks.push(chunk_event);
    }

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
    });
```
