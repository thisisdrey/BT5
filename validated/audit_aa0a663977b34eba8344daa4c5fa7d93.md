### Title
Unauthenticated `/stackerdb_chunks` HTTP endpoint on the signer's event receiver allows forging `SignerEvent::SignerMessages` with an arbitrary slot_id/pk pairing - ([File: libsigner/src/events.rs])

### Summary
The signer process's event-receiver HTTP server (`SignerEventReceiver`) is documented and configured to bind to `0.0.0.0` (see `sample/conf/signer/mainnet-signer-conf.toml`) yet performs no authentication on incoming HTTP POSTs. Any remote party who can reach that port can POST a crafted `StackerDBChunksEvent` JSON body to `/stackerdb_chunks`, which is deserialized and converted straight into `SignerEvent::SignerMessages` via `TryFrom<StackerDBChunksEvent>`, with the `(slot_id, pk, message)` tuple built from `chunk.recover_pk()` — a pk merely recovered from an attacker-supplied signature, never checked against the reward cycle's actual registered owner of `slot_id`.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` (lines 413-459) reads any inbound HTTP request on the bound socket and, for `POST /stackerdb_chunks`, calls `process_event::<T, StackerDBChunksEvent>(request)` with no authentication check anywhere in this path — no `Authorization` header, no `auth_password`/token comparison exists in `libsigner` (confirmed via search: zero matches for `auth_password`/`Authorization`/`auth_token` in `libsigner/**`). `process_event` (lines 519-542) just `serde_json::from_slice`s the raw body into `StackerDBChunksEvent` and calls `.try_into()`.

`TryFrom<StackerDBChunksEvent> for SignerEvent<T>` (lines 544-625), for signer-contract chunks, does:
```
let Ok(pk) = chunk.recover_pk() else { ... };
...
Some((chunk.slot_id, pk, message))
```
`chunk.recover_pk()` (in `libstackerdb/src/libstackerdb.rs`) recovers whatever public key is consistent with the attacker-supplied `sig` over the attacker-supplied `data` — it is a pure ECDSA recovery operation with no lookup against the actual StackerDB config/slot-owner list for that reward cycle. Thus the equality the question describes — "pk used to key the (slot_id, pk, message) tuple == the pubkey of the signer legitimately assigned to slot_id" — is not enforced anywhere in this code path. An attacker can pick any `slot_id`, generate their own keypair, sign an arbitrary payload matching the expected `SignerMessageTypePrefix`, and this function will happily produce a `(slot_id, attacker_pk, message)` tuple.

Normally this isn't reachable by outsiders because this event-receiver port is meant to only receive pushes from the node's own event-dispatcher (which is the one that actually validates a chunk's signature against the real slot owner when the chunk was PUT to StackerDB). But the shipped/reference configuration (`sample/conf/signer/mainnet-signer-conf.toml`, line 39: `endpoint = "0.0.0.0:30000"`) explicitly binds this port to all interfaces, and the HTTP server (`tiny_http`-based, `SignerEventReceiver::bind`) applies no source-IP restriction or authentication whatsoever. Any unprivileged remote party that can route packets to that port can therefore POST directly to `/stackerdb_chunks`, bypassing the node entirely and the real signature-provenance check that normally happens at StackerDB-write time.

### Impact Explanation
An attacker who can reach the bound event-receiver port can inject a fully forged `SignerEvent::SignerMessages` entry into the victim signer's runloop, with a `(slot_id, pk, message)` tuple whose `pk` does not correspond to the actual registered owner of `slot_id` for that reward cycle. This is an unauthenticated/unauthorized injection of state into the signer's internal message-processing/vote-tallying pipeline — the signer process treats attacker data as if it came from a legitimate, StackerDB-writing signer. This matches "unauthenticated/unauthorized write to state" and can corrupt vote tallying/signer state-machine views downstream in `stacks-signer`. It is trivially repeatable per message (each POST yields one crafted event).

### Likelihood Explanation
Preconditions: reachability of the signer's bound event-receiver TCP port (no privileged role, no secret, no local access needed) and knowledge of the target contract name format (`signer-<set>-<message_id>`) and a valid payload-type prefix byte, both public/documented. The reference/mainnet sample config binds this port to `0.0.0.0`, and there is no code-level auth gate at all in `libsigner/src/events.rs`, so the only thing standing between an internet attacker and this endpoint is network-level firewalling that is not enforced by the code itself. Attacker cost is a single crafted HTTP POST; fully repeatable.

### Recommendation
Add authentication to the signer's event-receiver HTTP server (e.g., require the same `auth_password`/token used for node RPC, or a shared HMAC/secret validated on every request) before processing any body, and/or bind by default to `127.0.0.1` and only relax to `0.0.0.0` with an explicit warning plus mandatory auth. Additionally, harden `TryFrom<StackerDBChunksEvent>` to cross-check `chunk.recover_pk()` against the actual reward-cycle signer set for `chunk.slot_id` before constructing `SignerEvent::SignerMessages`, rather than trusting that events only arrive from the local trusted node.

### Proof of Concept
```rust
// In libsigner/src/tests/mod.rs (or a new test module)
#[test]
fn test_process_event_accepts_unowned_slot_pk() {
    // 1. Generate an attacker keypair (unrelated to any registered signer for slot_id=0).
    let attacker_sk = StacksPrivateKey::random();
    let attacker_pk = StacksPublicKey::from_private(&attacker_sk);

    // 2. Build an arbitrary signer-message payload (e.g., a minimal StateMachineUpdate
    //    or BlockResponse with the correct type-prefix byte) and sign it with attacker_sk
    //    to produce a StackerDBChunkData { slot_id: 0, slot_version: 1, data, sig }.
    let chunk = StackerDBChunkData::new(0 /* slot_id */, 1 /* slot_version */, payload_bytes);
    chunk.sign(&attacker_sk).unwrap();

    // 3. Wrap it in a StackerDBChunksEvent for contract_id = "<boot addr>.signers-0-<msg_id>"
    let event = StackerDBChunksEvent {
        contract_id: signers_contract_id(0, msg_id),
        modified_slots: vec![chunk],
    };
    let body = serde_json::to_vec(&event).unwrap();

    // 4. POST this body to a running SignerEventReceiver bound on 0.0.0.0:<port>
    //    at path /stackerdb_chunks, exactly as an unprivileged remote client would.
    let signer_event: SignerEvent<SomeT> = event.try_into().unwrap();

    // 5. Assert the tuple's recovered pk is attacker_pk, NOT the real registered
    //    owner of slot_id 0 for this reward cycle - proving no slot-ownership check exists.
    if let SignerEvent::SignerMessages { messages, .. } = signer_event {
        let (slot_id, pk, _msg) = &messages[0];
        assert_eq!(*slot_id, 0);
        assert_eq!(*pk, attacker_pk); // succeeds - no ownership verification
        assert_ne!(*pk, real_registered_signer_pk_for_slot_0); // proves the mismatch
    } else {
        panic!("expected SignerMessages");
    }
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

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

**File:** libsigner/src/events.rs (L580-619)
```rust
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
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L33-50)
```text
# REQUIRED: The Stacks node RPC endpoint to connect to.
# Must match the node's [node] rpc_bind address.
node_host = "127.0.0.1:20443"

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
