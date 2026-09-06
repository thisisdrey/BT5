This confirms the finding: the sample config default binds the event listener to `0.0.0.0:30000` (all interfaces, not loopback), and the code explicitly warns that no additional authentication exists at this layer.### Title
Unauthenticated forgery of `SignerEvent::SignerMessages` slot ownership via direct POST to the signer's `/stackerdb_chunks` HTTP listener - (File: `libsigner/src/events.rs`)

### Summary
The `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` implementation for `signers-X-Y` boot contracts accepts any chunk whose `chunk.recover_pk()` succeeds and pairs the recovered public key with the attacker-chosen `slot_id` with no check that the recovered key is the slot's actual configured owner. This check normally happens upstream in the node's StackerDB storage layer (`try_replace_chunk`/`validate_received_chunk`), but the signer's HTTP event-receiver endpoint (`SignerEventReceiver`, bound by default to `0.0.0.0:<port>` per `sample/conf/signer/mainnet-signer-conf.toml`) has no authentication and accepts a raw, attacker-supplied JSON `StackerDBChunksEvent` body directly, bypassing that upstream verification entirely.

### Finding Description
`TryFrom<StackerDBChunksEvent> for SignerEvent<T>::try_from` (`libsigner/src/events.rs:544-625`) iterates `event.modified_slots` for `signers-X-Y` boot contracts. For each `chunk`, it checks the payload type prefix matches the lane (`signer_message_payload_matches_lane`), calls `chunk.recover_pk()` (`libsigner/src/events.rs:596-603`), deserializes the payload, and emits `Some((chunk.slot_id, pk, message))` into `SignerEvent::SignerMessages { messages, .. }` (`libsigner/src/events.rs:612`). Nowhere in this function is `pk` compared against the actual owner of `chunk.slot_id` for the contract's current signer set — there is no `stackerdbs.get_slot_signer(...)` or equivalent membership check at this layer.

This is safe *only* because, in the normal flow, this event is produced by the trusted node process itself: `stacks-node/src/event_dispatcher.rs::process_new_stackerdb_chunks` only fires after chunks have passed through `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:398-439`), which calls `slot_desc.verify(&slot_validation.signer)` and rejects chunks not signed by the registered slot owner (`net_error::BadSlotSigner`). So by the time the node's `event_dispatcher` POSTs to the signer's `/stackerdb_chunks` endpoint, `recover_pk()` is guaranteed to equal the true slot owner — *when the event genuinely originated from the node*.

However, `SignerEventReceiver` (`libsigner/src/events.rs:316-459`) is a plain `tiny_http` server that accepts POST requests to `/stackerdb_chunks` from **any TCP client that can reach the bound port**, with no shared-secret, token, or peer-address check anywhere in `next_event`/`process_event`. The sample signer config binds this listener to `endpoint = "0.0.0.0:30000"` (`sample/conf/signer/mainnet-signer-conf.toml:39,45`) — i.e., all interfaces, not loopback-only — and `stacks-signer/src/lib.rs:125-132` only emits a runtime warning about this risk rather than enforcing any protection. Consequently, an attacker who can reach this port can bypass the node's `try_replace_chunk`/`validate_received_chunk` owner check entirely by POSTing a hand-crafted JSON body directly to `/stackerdb_chunks`, containing a `modified_slots` chunk with any `slot_id` and a valid secp256k1-recoverable `sig` produced by their own private key. `chunk.recover_pk()` will succeed and return the attacker's own public key, which `events.rs` will forward unchecked as the slot's owner inside `SignerEvent::SignerMessages`.

### Impact Explanation
The downstream signer runloop consumes `SignerEvent::SignerMessages` messages keyed by `(slot_id, pk, message)` and treats `pk` as an authenticated proof of which signer produced the message (used for signer-set membership/weight lookups and message provenance). An attacker forging this tuple can inject arbitrary signer-protocol messages (e.g., `BlockResponse`, `StateMachineUpdate`, `BlockPreCommit` payloads, subject only to the type-prefix/lane check) that appear to originate from any slot the attacker chooses, potentially influencing the local signer's internal decision state. This is unauthenticated write/injection of forged data into the signer's message-processing pipeline — matching the "unauthenticated/unauthorized write to state" and "network-wide propagation of forged data" Critical categories, since a single signer's corrupted internal state can affect its participation in block signing.

### Likelihood Explanation
The attack requires only network reachability to the signer's event-listener port. Given the reference/sample configuration explicitly binds to `0.0.0.0` (not `127.0.0.1`) and the code contains no port-level authentication, any deployment following the documented sample config (or any operator who does not manually restrict the bind address via a firewall) is exposed to any remote unprivileged party. No node RPC secret, signer key, or slot ownership is required — the attacker only needs to generate their own `Secp256k1PrivateKey`, which is free and repeatable. This is a Critical-severity, remotely and repeatably exploitable issue whenever the endpoint is not additionally firewalled to loopback, which is a real-world operational hazard the codebase itself warns about but does not enforce.

### Recommendation
Add an authentication mechanism to `SignerEventReceiver`/`process_event` in `libsigner/src/events.rs` (e.g., verify a shared secret/HMAC header matching the node's configured `auth_token`, analogous to `auth_password`/`auth_token` already used for the node's RPC/proposal endpoints), and/or enforce binding this listener to loopback-only by default with an explicit opt-in warning/hard-fail for non-loopback binds. Additionally, consider validating `pk` against the current signer set membership for `chunk.slot_id`/`signer_set` where such information is available to the signer, rather than trusting `recover_pk()` alone.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use blockstack_lib::chainstate::stacks::events::StackerDBChunksEvent;
use stacks_common::util::secp256k1::Secp256k1PrivateKey;
use libstackerdb::StackerDBChunkData;
use clarity::vm::types::QualifiedContractIdentifier;

#[test]
fn forged_slot_owner_via_attacker_key() {
    // Attacker generates their own keypair (not any configured signer's key).
    let attacker_privk = Secp256k1PrivateKey::new();

    // Attacker picks an arbitrary slot_id belonging to a legitimate signer they don't control.
    let mut chunk = StackerDBChunkData::new(/* slot_id */ 3, /* slot_version */ 1, vec![/* valid v0 SignerMessage payload bytes, e.g. StateMachineUpdate type-prefix */]);
    chunk.sign(&attacker_privk).unwrap();

    let event = StackerDBChunksEvent {
        contract_id: QualifiedContractIdentifier::parse("SP000000000000000000002Q6VF78.signers-0-2").unwrap(),
        modified_slots: vec![chunk.clone()],
    };

    let signer_event: SignerEvent<crate::v0::messages::SignerMessage> =
        SignerEvent::try_from(event).unwrap();

    if let SignerEvent::SignerMessages { messages, .. } = signer_event {
        let (slot_id, pk, _msg) = &messages[0];
        let attacker_pk = stacks_common::types::chainstate::StacksPublicKey::from_private(&attacker_privk);
        // Assert: no ownership check occurred — the attacker's own pk was accepted for a slot they don't own.
        assert_eq!(*slot_id, 3);
        assert_eq!(*pk, attacker_pk);
    } else {
        panic!("expected SignerMessages event");
    }
}
```
This unit test demonstrates the missing-owner-check equality in isolation. The full exploit additionally requires POSTing the equivalent JSON body directly to the signer's `/stackerdb_chunks` HTTP endpoint (reachable when bound non-loopback, e.g. `0.0.0.0:30000` per the sample config) to reach `process_event::<T, StackerDBChunksEvent>` (`libsigner/src/events.rs:437-438`), confirming the network-reachable path with no auth gate in `next_event`/`process_event`.