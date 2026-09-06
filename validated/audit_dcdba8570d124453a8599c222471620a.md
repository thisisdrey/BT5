I found this - `SignerEventReceiver::next_event` in `libsigner/src/events.rs` accepts POST requests on `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` with **no authentication check at all** in the receiver itself, despite documentation (`docs/signing.md`) describing an `auth_token`/`auth_password` mechanism that is supposed to gate this channel.### Title
Unauthenticated write to signer runloop via `SignerEventReceiver` HTTP listener (missing `auth_token` check) - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver::next_event` handler that receives event pushes from a Stacks node (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) accepts and processes any POST request from any TCP peer that can reach the signer's listening endpoint, without verifying any shared secret / auth token, even though the documented deployment model treats this channel as authenticated via a shared `auth_token`/`auth_password`.

### Finding Description
`docs/signing.md` instructs operators to configure a shared secret (`[connection_options] auth_token` on the node, `auth_password` on the signer) that "must match" between the two sides [1](#0-0) . This implies the signer is meant to trust only events originating from its paired, authenticated node.

However, `SignerEventReceiver::next_event` (`libsigner/src/events.rs`) binds an HTTP server and, on any incoming POST, dispatches directly to `process_event` for `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` with no header/token verification of any kind: [2](#0-1) 

`process_event` only reads the body and deserializes JSON into a `StackerDBChunksEvent` / `BlockValidateResponse` / etc., again with no authentication step: [3](#0-2) 

Once accepted, a `StackerDBChunksEvent` for the `signer-XXX-YYY` boot contract is converted into `SignerEvent::SignerMessages`, which is forwarded straight into the signer runloop (`forward_event` → `main_loop`) as if it had genuinely come from the node's StackerDB replication pipeline: [4](#0-3) [5](#0-4) 

This breaks the intended equality "message came from the paired node" vs. "message accepted by the signer" — the receiver fails open: any reachable network peer, without knowing `auth_token`, can POST directly to the signer's endpoint and inject forged `StackerDBChunksEvent`, `BlockValidateResponse`, or `BurnBlockEvent` payloads. Note that individual `StackerDBChunkData` items still carry their own slot signatures (checked later via `chunk.recover_pk()` in the signer-message path [6](#0-5) ), so raw signer-message forgery is still gated by that per-chunk signature; but `/proposal_response` and `/new_burn_block`/`/new_block` events carry no such downstream signature check in this file, and the HTTP transport itself has no authentication gate protecting which processes may deliver these events in the first place.

### Impact Explanation
This is a transport-layer auth-gate that fails open: the code that is supposed to only accept events from the operator's own trusted node instead accepts unauthenticated input from any peer that can reach the listening port. Depending on deployment (binding on `0.0.0.0` per the sample configs, e.g. `sample/conf/mainnet-miner-conf.toml`), this could allow an unprivileged remote party to inject fabricated block-validation responses or burn-block/new-block events into the signer's processing pipeline, since nothing in this receiver enforces the `auth_token`/`auth_password` pairing described in the docs.

### Likelihood Explanation
Likelihood depends on network exposure of the signer's HTTP listener; the documented deployment guidance (binding to `0.0.0.0` in sample configs, matching `auth_token`) suggests operators are expected to rely on this shared secret for protection, but the code that should enforce it does not check anything, so any deployment that exposes the port beyond localhost is immediately exploitable by an unauthenticated network client.

### Recommendation
Have `SignerEventReceiver::next_event` (or `process_event`) validate an `Authorization`/custom header against the configured `auth_password` before deserializing/dispatching any event, mirroring the auth-token check already present on the node's `postblock_proposal.rs`/`httpcore.rs` path for the reverse direction.

### Proof of Concept
Any TCP client that can reach the signer's bound address can reproduce the acceptance path demonstrated by the existing test harness — simply open a socket and POST a crafted `StackerDBChunksEvent`/`BlockValidateResponse` JSON body to `/stackerdb_chunks` or `/proposal_response` without any authentication header, exactly as done (for legitimate testing purposes) in: [7](#0-6)  — the receiver has no code path to reject this even from an unrecognized/unauthenticated sender.

### Citations

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

**File:** libsigner/src/events.rs (L282-312)
```rust
    /// Main loop for the receiver.
    /// Typically, this is started in a separate thread.
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
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
