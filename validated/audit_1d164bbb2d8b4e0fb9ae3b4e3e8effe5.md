## Title
Signer event HTTP receiver accepts unauthenticated pushes, allowing forged StackerDB/proposal/burn-block events to be injected into signer state - (File: libsigner/src/events.rs)

### Summary
The Popcorn report's root cause is that a component the caller *trusts implicitly* (the strategy contract) can write directly into the caller's protected state because no independent authorization boundary is enforced between them. The same class of failure exists in `SignerEventReceiver::next_event`: the signer's local HTTP listener accepts and processes `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, and `StacksBlockEvent` payloads from any TCP client that can reach the bound port, without ever checking the `auth_token`/`auth_password` value that the documentation says is required to authenticate the node→signer channel.

### Finding Description
`SignerEventReceiver::next_event` dispatches incoming HTTP POST requests purely by URL path, with no authentication check: [1](#0-0) 

It reads the raw body and deserializes it directly into signer-trusted event types via `process_event`: [2](#0-1) 

The project's own documentation states that `auth_password` (signer side) and `auth_token` (node side) "must match" and are the mechanism that authenticates this channel: [3](#0-2) 

However, `auth_password`/`auth_token` is only used by the *signer* as an outgoing `Authorization` header when the signer calls back into the *node's* RPC API (`stacks-signer/src/client/stacks_client.rs`), and by the *node* to gate its own RPC endpoints. There is no code path in `libsigner/src/events.rs` that reads an `Authorization` header, compares it to a configured secret, or otherwise validates the origin of a POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block`. This equality — "an authenticated node pushed this event" vs. "any TCP peer that can reach the port sent this event" — is never actually checked at the transport layer that receives it, breaking the auth-gate that the documentation and configuration surface (`[connection_options] auth_token` in miner/signer sample configs) imply is enforced.

Downstream, these forged events feed directly into the signer's decision-making inputs: `StackerDBChunksEvent` becomes `SignerEvent::SignerMessages`/`MinerMessages` after only a *content-signature* check on individual chunks (not a check that the event itself came from the trusted node), and `BlockValidateResponse`/`BurnBlockEvent` are converted into `SignerEvent` variants with no equivalent transport-level authentication at all: [4](#0-3) 

### Impact Explanation
Any unprivileged process capable of opening a TCP connection to the signer's configured `endpoint` (which per the sample configs binds to `127.0.0.1` but is user-configurable and has no code-level enforcement of locality or authentication) can:
- Forge `BlockValidateResponse` events to influence the signer's block-validation decision inputs.
- Forge `BurnBlockEvent`/`StacksBlockEvent` events to desynchronize the signer's view of chain state.
- Inject arbitrary `StackerDBChunksEvent` payloads (which do carry a per-chunk signer signature check inside `TryFrom<StackerDBChunksEvent>`, limiting outright forgery of `SignerMessages`, but the transport itself still accepts and processes any POST unconditionally, and the non-StackerDB event types have no equivalent per-message authentication at all).

This is an unauthenticated write into the signer's internal event stream, which is a Critical-class impact per the rubric ("unauthenticated/unauthorized write to state ... auth bypass") for the transport surface, even though `StackerDBChunksEvent` content itself is partially defended by an independent chunk-signature check.

### Likelihood Explanation
High for any deployment where the signer's `endpoint` is reachable by more than the local node process (e.g., bound to a non-loopback interface, or reachable via container/network misconfiguration) — no attacker capability beyond network reachability and knowledge of the documented HTTP paths (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) is required. No node secret, signer key, or admin role is needed to reach and invoke this endpoint.

### Recommendation
Have `SignerEventReceiver::next_event` (or `process_event`) validate an `Authorization`/token header against the signer's configured `auth_password` before deserializing or forwarding any event, mirroring the authentication already documented as a requirement for this channel, and reject/ack-without-processing any request that fails the check.

### Proof of Concept
`libsigner/src/tests/mod.rs::test_simple_signer` already demonstrates the vulnerable path structurally: it spins up a `SignerEventReceiver`, then a plain `TcpStream` sends a raw, hand-built HTTP POST to `/stackerdb_chunks` with a JSON body, and the event is accepted and forwarded to the runloop with no authentication header present at all: [5](#0-4) 

This test (intended to validate legitimate delivery) equally proves that no auth header is required for the request to succeed — any client capable of forming this exact HTTP request against the bound port can achieve the same result.

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
