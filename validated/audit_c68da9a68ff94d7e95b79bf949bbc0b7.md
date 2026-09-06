### Title
Unauthenticated `SignerEventReceiver` HTTP endpoint accepts forged `StackerDBChunksEvent` chunks without verifying `recover_pk()` result against the real slot owner - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::bind` opens a plain `tiny_http::HttpServer` with no shared secret, token, or peer-identity check, and `next_event`/`process_event` accept any POST to `/stackerdb_chunks` and deserialize it into a `StackerDBChunksEvent` via `serde_json::from_slice`. The `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` implementation only checks that `chunk.recover_pk()` succeeds (i.e., that the `sig` field is *some* valid ECDSA signature), never comparing the recovered public key to the actual owner of `chunk.slot_id` for `event.contract_id`, so a self-signed chunk from an attacker-controlled key is forwarded as a legitimate `SignerEvent::SignerMessages` entry.

### Finding Description
The broken equality is: `pk` returned by `chunk.recover_pk()` at [1](#0-0)  is never checked against the address that legitimately owns `event.contract_id`'s `slot_id`. Nothing in `libsigner/src/events.rs` has access to (or consults) a slot-owner/reward-set table; the function only verifies "is this a syntactically valid, recoverable ECDSA signature," not "did the real slot owner sign this."

The reachable path is:
1. `SignerEventReceiver::bind` starts an `HttpServer::http(listener)` with zero authentication, no shared-secret header check, no IP allowlist [2](#0-1) .
2. `next_event` dispatches any POST to `/stackerdb_chunks` straight into `process_event::<T, StackerDBChunksEvent>(request)` [3](#0-2) .
3. `process_event` reads the raw body and calls `serde_json::from_slice` to build a `StackerDBChunksEvent`, then `.try_into()` [4](#0-3) .
4. `TryFrom<StackerDBChunksEvent>` iterates `event.modified_slots`, checks the payload-type-prefix byte matches the contract "lane," calls `chunk.recover_pk()`, and — critically — accepts whatever public key comes back without checking it against a real slot-owner registry, then emits `(chunk.slot_id, pk, message)` into `SignerEvent::SignerMessages` [5](#0-4) .

An attacker who can open a TCP connection to this socket can craft a `StackerDBChunkData` with `slot_id`/`slot_version` of their choosing, a payload whose first byte matches a valid `SignerMessageTypePrefix` for a chosen `signers-X-Y` lane, and a signature produced with a throwaway keypair (`recover_to_pubkey_without_validating_low_s` only needs a well-formed, low-or-high-S signature over the chunk's signed digest — it does not need to correspond to any registered signer). This bypasses the real StackerDB PUT-chunk path entirely (which does check the writer's signature against the actual slot owner in the node's StackerDB implementation) because the attacker talks directly to the signer's HTTP listener, not through the node's StackerDB write RPC.

### Impact Explanation
A successful POST causes the signer process to receive a `SignerEvent::SignerMessages` tuple `(slot_id, attacker_pk, message)` that is indistinguishable, at this layer, from a message legitimately written by a real slot owner. This is unauthenticated content injected into the signer's internal event stream/runloop, corresponding to "unauthenticated/unauthorized write to state or forged data injected into a trusted internal pipeline." The attack is trivially repeatable (each TCP POST is a fresh forged event) and costs the attacker nothing beyond generating an ECDSA keypair and signature.

### Likelihood Explanation
Preconditions: the attacker only needs network reachability to the TCP port that `SignerEventReceiver::bind` listens on (a precondition explicitly given in the question). No node RPC secret, no P2P handshake, no legitimate slot ownership, and no local/physical access are required — `bind`/`next_event` perform no authentication whatsoever before parsing and trusting the JSON body. This makes exploitation low-cost and fully remote once network reachability is established.

### Recommendation
Add authentication to the `SignerEventReceiver` HTTP listener (e.g., a shared secret/bearer token configured between the co-located node and signer, checked in `next_event` before dispatching to `process_event`), and/or have `TryFrom<StackerDBChunksEvent>` validate `chunk.recover_pk()`'s result against the actual current slot-owner address for `(event.contract_id, chunk.slot_id)` (e.g., by querying the reward-set/signer-set the signer already tracks) before constructing `SignerEvent::SignerMessages`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) - conceptual PoC
#[test]
fn forged_chunk_bypasses_slot_owner_check() {
    use libstackerdb::StackerDBChunkData;
    use blockstack_lib::chainstate::stacks::events::StackerDBChunksEvent;
    use clarity::vm::types::QualifiedContractIdentifier;

    // 1. Attacker generates a throwaway keypair, not registered as any slot owner.
    let attacker_privk = StacksPrivateKey::random();

    // 2. Craft a payload with a valid type-prefix byte for lane message_id=1 (BlockResponse),
    //    e.g. minimal well-formed SignerMessage bytes.
    let payload: Vec<u8> = vec![/* type_byte + minimal valid encoding */];

    // 3. Build and sign a StackerDBChunkData with attacker's own key (self-generated sig,
    //    never checked against any real slot-owner set).
    let mut chunk = StackerDBChunkData::new(0 /*slot_id*/, 1 /*slot_version*/, payload);
    chunk.sign(&attacker_privk).unwrap();

    let event = StackerDBChunksEvent {
        contract_id: QualifiedContractIdentifier::parse(
            "SP000000000000000000002Q6VF78.signers-0-1"
        ).unwrap(),
        modified_slots: vec![chunk],
    };

    // 4. Feed directly through the same conversion process_event would use.
    let signer_event: SignerEvent<crate::v0::messages::SignerMessage> =
        event.try_into().unwrap();

    if let SignerEvent::SignerMessages { messages, .. } = signer_event {
        // The attacker's throwaway pubkey is accepted with no check against a real
        // slot-owner address for slot_id 0 in this contract.
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].1, StacksPublicKey::from_private(&attacker_privk));
    } else {
        panic!("expected SignerMessages");
    }
}
```
This test demonstrates that `TryFrom<StackerDBChunksEvent>` (and, by extension, a raw HTTP POST to `/stackerdb_chunks` on the bound socket) accepts and forwards a chunk whose signature was never checked against any real slot-owner identity — matching the exact fault described in the question.

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L436-438)
```rust
            debug!("Processing {} event", request.url());
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
```

**File:** libsigner/src/events.rs (L524-541)
```rust
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
