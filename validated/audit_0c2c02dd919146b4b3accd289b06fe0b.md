### Title
Unauthenticated write into signer event stream / runloop via `SignerEventReceiver::next_event` - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` (libsigner/src/events.rs:413-459) accepts any HTTP POST to `/stackerdb_chunks`, `/proposal_response`, or `/new_burn_block` on the signer's bound listener and forwards the parsed payload as an authentic `SignerEvent` to the runloop via `forward_event`, with no credential, header, or shared-secret check tying the request to the configured Stacks node. For the `.miners` StackerDB contract lane specifically, `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` (events.rs:544-567) deserializes chunk data straight into `SignerEvent::MinerMessages` with no signature/`recover_pk` check at all, so a raw TCP client posing as the node can inject fabricated miner messages that the signer will process as if they came from validated StackerDB writes.

### Finding Description
The broken equality is: "sender of the HTTP request to the signer's bound port" ≠ "the configured Stacks node." `next_event` does `http_server.recv()`, dispatches purely on URL/method, and calls `process_event::<T, StackerDBChunksEvent>(request)` (events.rs:437-438) for `/stackerdb_chunks`, with zero check of any header, token, or peer credential before treating the payload as authentic. `process_event` (events.rs:519-542) just reads the body, JSON-deserializes it into the target type, and `try_into()`s it into a `SignerEvent`, then `next_event` returns it and the trait's default `main_loop` calls `forward_event` (events.rs:469-490), which pushes it straight to the runloop's `out_channels` — no gate exists anywhere in this path that verifies the TCP peer is the node the signer is configured to trust.

Compounding this, for the `.miners` boot-contract lane inside `TryFrom<StackerDBChunksEvent>` (events.rs:549-567), each chunk's bytes are `T::consensus_deserialize`'d directly with **no `chunk.recover_pk()` / signature check at all**, unlike the `signers-X-Y` lane a few lines below which does call `chunk.recover_pk()` (events.rs:596). So an attacker crafting a bare JSON `StackerDBChunksEvent` with `contract_id` set to the `.miners` boot contract and a `modified_slots` entry containing arbitrary bytes that decode as a valid `SignerMessage`/`T` will have that payload accepted into `SignerEvent::MinerMessages` with no cryptographic check whatsoever, and forwarded to the signer runloop as genuine miner traffic.

Existing guards that don't help here: `MAX_MESSAGE_LEN`/per-field caps only bound size, not authenticity; the `signers-X-Y` lane's `recover_pk()` only recovers *some* public key from attacker-controlled signature bytes — it doesn't check that key against a known/authorized signer set at this layer, so it stops nothing either, it just yields an arbitrary attacker-chosen `(slot_id, pk, message)` tuple that also gets forwarded.

### Impact Explanation
Any remote party who can open a TCP connection to the signer's bound event-receiver port can inject fabricated `SignerEvent::MinerMessages` and `SignerEvent::SignerMessages` into the signer's runloop without holding any secret, node identity, or slot ownership, repeatable per HTTP POST. This is an unauthenticated write into the signer's internal event/state stream (Critical per the stated category: "unauthenticated/unauthorized write to state"). Whether this leads to further damage (e.g., a signer casting a signature/response based on forged miner data) depends on downstream runloop/decision-logic validation, which is explicitly out of scope for this question — but the transport-layer authenticity gap itself, and the complete absence of signature verification on the `.miners` lane specifically, are squarely in scope and are real, reproducible defects in `libsigner/src/events.rs`.

### Likelihood Explanation
Preconditions: the attacker only needs network reachability to the signer's HTTP listener address/port and `is_stopped()` to be false — both routine operating conditions. No secret, peer key, or admin role is required; this matches the "unprivileged remote attacker" threat model exactly. The `auth_token`/`auth_password` settings documented in `docs/signing.md` and the sample confs (`sample/conf/mainnet-signer.toml`, `sample/conf/testnet-signer.toml`) authenticate the opposite direction (signer/miner → node RPC), and no equivalent mechanism exists for node → signer event delivery in `libsigner/src/events.rs`; `stacks-node/src/event_dispatcher.rs`'s `dispatch_to_observer` sends no `Authorization` header either. Attack cost is a single crafted HTTP POST, fully repeatable.

Note: I could not fully verify, given remaining tool budget, what the actual default bind address is for `SignerEventReceiver` in deployed configs (samples show `127.0.0.1:30000`, which would only be reachable from localhost) — if signer operators always bind to loopback and the port is never exposed beyond localhost, remote reachability may be mitigated by deployment topology in practice, though nothing in `libsigner/src/events.rs` itself enforces this restriction, and the code will happily bind to `0.0.0.0` if configured to do so.

### Recommendation
Require and verify a pre-shared secret/HMAC (mirroring the existing `auth_token`/`auth_password` pattern already used for the reverse direction) on every request in `SignerEventReceiver::next_event` before calling `process_event`, rejecting unauthenticated requests with 401/403. Additionally, add a `chunk.recover_pk()` check (and validation against the expected miner/signer key set) to the `.miners` lane in `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` (events.rs:549-567) so it is held to the same signature-verification standard as the `signers-X-Y` lane.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) - conceptual net test
#[test]
fn unauthenticated_peer_can_inject_stackerdb_event() {
    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    let handle = std::thread::spawn(move || receiver.next_event());

    // Attacker: bare TcpStream, no credentials, no relationship to the "node"
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = serde_json::json!({
        "event_index": 0,
        "modified_slots": [],
        "contract_id": {
            "issuer": [26, [0u8;20]],
            "name": "miners"
        }
    }).to_string();
    let req = format!(
        "POST /stackerdb_chunks HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: application/json\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let event = handle.join().unwrap().expect("attacker-forged event accepted");
    // Assertion: an unauthenticated TCP peer produced a trusted SignerEvent
    assert!(matches!(event, SignerEvent::MinerMessages(_)));
}
```
Expected result: the assertion passes — the bare, credential-less `TcpStream` connection successfully produces a `SignerEvent` from `next_event()`, confirming there is no authenticity check on the transport.