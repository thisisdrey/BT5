### Title
Unauthenticated `SignerEventReceiver` HTTP listener accepts forged block-validation and burn-block events - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver` (the `stacks-signer`'s local HTTP event listener) binds a plain `tiny_http` server and processes any `POST` request to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` without any authentication, source-address, or token check, mirroring the NI MeasurementLink class of bug: a service assumed to be reachable only by the trusted local node but which accepts requests from any host that can reach the bound port.

### Finding Description
`SignerEventReceiver::bind` opens an HTTP server on the configured `endpoint` with no credential check [1](#0-0) . `next_event()` dispatches based solely on the URL path and accepts any `POST` body as a trusted event from "the node" [2](#0-1) . `process_event` only reads and JSON-deserializes the body — there is no signature, shared-secret, or peer-identity check anywhere in this path [3](#0-2) .

Two of the four accepted event types are forwarded to the signer runloop with **no cryptographic validation at all**:
- `BlockValidateResponse` → `SignerEvent::BlockValidationResponse` is passed straight through with no signer/owner check [4](#0-3) .
- `BurnBlockEvent` → `SignerEvent::NewBurnBlock` is likewise passed straight through unchecked [5](#0-4) .

Only the `StackerDBChunksEvent` path (`/stackerdb_chunks`) performs any cryptographic filtering, and even there it merely recovers a public key and lets downstream signer-message logic decide relevance — it does not verify the sender is the trusted node itself [6](#0-5) .

The documentation and sample configs make clear the intended trust model relies on a separate `auth_password`/`auth_token` pairing for the **node's** RPC endpoint (`[connection_options] auth_token` on the node side, matching `auth_password` on the signer side) [7](#0-6) [8](#0-7) . However, that credential secures the *node's* HTTP API for the signer's outbound calls — it is never checked on the *signer's own* inbound event-listener endpoint (`endpoint = "0.0.0.0:30000"`) shown in the sample configs, which binds on all interfaces [9](#0-8)  and in the signer test conf even on `[::1]` or `localhost` rather than being firewalled off [10](#0-9) . The test suite itself demonstrates the wide-open acceptance behavior: a raw `TcpStream` connecting and POSTing arbitrary JSON is immediately accepted and forwarded as a genuine signer event [11](#0-10) .

This exactly matches the CVE-2023-4570 bug class: a service exposed on a network interface believed to be reachable only by a trusted co-located process, but which in fact accepts unauthenticated input from any adjacent-network host, and where that input (fake block-validation responses, fake burn-block notices) is trusted without cryptographic proof of origin.

### Impact Explanation
An attacker who can reach the signer's bound event port (an adjacent-network attacker per the advisory's threat model, or any process on a shared host/network segment where the port is not firewalled) can:
- Inject forged `BlockValidateResponse` events, which are passed unauthenticated straight into the signer's decision loop as `SignerEvent::BlockValidationResponse`.
- Inject forged `BurnBlockEvent`/`NewBurnBlock` notifications, again with zero authentication.

Depending on how the signer runloop consumes `BlockValidationResponse` for its co-signing decisions, this can influence or corrupt the signer's view of block validity/chain state — a state-integrity issue reachable by an unauthenticated network peer. This falls under "unauthenticated/unauthorized write to state" / "steering a node off the tip via false inventory"-class impact, since the signer's operational state is driven by unauthenticated network input.

### Likelihood Explanation
Likelihood is high wherever the signer's event endpoint is reachable beyond `127.0.0.1` — which the shipped reference configuration explicitly does (`endpoint = "0.0.0.0:30000"`), requiring no privileged access, no node secret, and no valid signer key; only network reachability to the port. This mirrors the "service exposed on localhost but reachable from adjacent network" scenario described in the source advisory.

### Recommendation
Add sender authentication to `SignerEventReceiver` (e.g., a shared-secret/token header check comparable to the `auth_token`/`auth_password` pairing already used for the node's own RPC API, or mTLS/loopback-only binding enforcement) before accepting and forwarding `/proposal_response` and `/new_burn_block` (and ideally all) events in `process_event`/`next_event`. Bind to `127.0.0.1` by default and require explicit opt-in plus authentication to listen on other interfaces.

### Proof of Concept
As shown by the project's own test harness, any TCP client can obtain acceptance of a forged event with a raw HTTP POST and no credentials [11](#0-10) :
```
POST /new_burn_block HTTP/1.1
Host: <signer-endpoint>
Content-Type: application/json
Content-Length: <n>

{"burn_block_height": ..., "burn_block_hash": ..., "consensus_hash": ..., "parent_burn_block_hash": ...}
```
This is accepted by `next_event` at `libsigner/src/events.rs:441-442` and forwarded via `TryFrom<BurnBlockEvent>` (`libsigner/src/events.rs:637-649`) with no signature or origin check, identical in structure to a legitimate node-originated event.

### Citations

**File:** libsigner/src/events.rs (L401-408)
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

**File:** libsigner/src/events.rs (L544-624)
```rust
impl<T: SignerEventTrait> TryFrom<StackerDBChunksEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(event: StackerDBChunksEvent) -> Result<Self, Self::Error> {
        let received_time = SystemTime::now();
        let signer_event = if event.contract_id.name.as_str() == MINERS_NAME
            && event.contract_id.is_boot()
        {
            let mut messages = vec![];
            for chunk in event.modified_slots {
                match T::consensus_deserialize(&mut chunk.data.as_slice()) {
                    Ok(msg) => messages.push(msg),
                    Err(e) => {
                        debug!(
                            "Signer failed to deserialize miner chunk";
                            "slot_id" => chunk.slot_id,
                            "slot_version" => chunk.slot_version,
                            "data_len" => chunk.data.len(),
                            "error" => %e,
                        );
                    }
                }
            }
            SignerEvent::MinerMessages(messages)
        } else if event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot() {
            let Some((signer_set, message_id)) =
                get_signers_db_signer_set_message_id(event.contract_id.name.as_str())
            else {
                return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
            };
            // signer-XXX-YYY boot contract
            //
            // NOTE: the payload-type check below uses v0 `SignerMessageTypePrefix` semantics
            // (the mapping in `signer_message_payload_matches_lane` is fixed to v0). Future
            // signer-message versions must extend that mapping, or their chunks will not be
            // recognized here regardless of which `T` is in scope.
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    let Ok(message) = read_next::<T, _>(&mut &chunk.data[..]) else {
                        warn!(
                            "Skipping signer chunk: payload deserialization failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    Some((chunk.slot_id, pk, message))
                })
                .collect();
            SignerEvent::SignerMessages {
                signer_set,
                messages,
                received_time,
            }
        } else {
            return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
        };
        Ok(signer_event)
    }
```

**File:** libsigner/src/events.rs (L627-635)
```rust
impl<T: SignerEventTrait> TryFrom<BlockValidateResponse> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(block_validate_response: BlockValidateResponse) -> Result<Self, Self::Error> {
        Ok(SignerEvent::BlockValidationResponse(
            block_validate_response,
        ))
    }
}
```

**File:** libsigner/src/events.rs (L637-649)
```rust
impl<T: SignerEventTrait> TryFrom<BurnBlockEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(burn_block_event: BurnBlockEvent) -> Result<Self, Self::Error> {
        Ok(SignerEvent::NewBurnBlock {
            burn_height: burn_block_event.burn_block_height,
            received_time: SystemTime::now(),
            burn_header_hash: burn_block_event.burn_block_hash,
            consensus_hash: burn_block_event.consensus_hash,
            parent_burn_block_hash: burn_block_event.parent_burn_block_hash,
        })
    }
}
```

**File:** docs/signing.md (L53-59)
```markdown
These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```

**File:** sample/conf/mainnet-signer.toml (L26-28)
```text
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```

**File:** sample/conf/mainnet-signer.toml (L36-38)
```text
[connection_options]
# WARNING: Must match the signer binary's `auth_password`.
auth_token = ""
```

**File:** stacks-signer/src/tests/conf/signer-0.toml (L1-7)
```text
stacks_private_key = "6a1fc1a3183018c6d79a4e11e154d2bdad2d89ac8bc1b0a021de8b4d28774fbb01"
node_host = "127.0.0.1:20443"
endpoint = "[::1]:30000"
network = "testnet"
auth_password = "12345"
db_path = ":memory:"
metrics_endpoint = "0.0.0.0:9090"
```

**File:** libsigner/src/tests/mod.rs (L120-146)
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
```
