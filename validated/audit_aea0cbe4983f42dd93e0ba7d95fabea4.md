### Title
Unauthenticated `/stackerdb_chunks` POST forges `SignerEvent::MinerMessages` with zero chunk-signature verification - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event()` accepts any POST to `/stackerdb_chunks` on the signer's bound HTTP listener and passes the JSON body straight to `TryFrom<StackerDBChunksEvent>` with no peer/origin/auth check. For chunks whose `contract_id.name == MINERS_NAME`, the code deserializes `chunk.data` directly into `T` and pushes it into `SignerEvent::MinerMessages` without ever calling `chunk.recover_pk()` or otherwise verifying who signed the StackerDB slot.

### Finding Description
In `next_event()` (`libsigner/src/events.rs:413-459`), the receiver dispatches any POST to `/stackerdb_chunks` to `process_event::<T, StackerDBChunksEvent>(request)` with no check of the request's origin, no shared-secret/HMAC/Authorization header check, and no TLS/mTLS peer identity check [1](#0-0) . `process_event` reads the raw body, JSON-decodes it into `StackerDBChunksEvent`, and calls `.try_into()` [2](#0-1) .

In `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`, the miner branch (triggered when `contract_id.name == MINERS_NAME && contract_id.is_boot()`) iterates `event.modified_slots` and calls `T::consensus_deserialize(&mut chunk.data.as_slice())` directly, pushing every successfully-parsed message into `SignerEvent::MinerMessages(messages)` — with no signature check on `chunk` at all [3](#0-2) . This is asymmetric with the adjacent `SIGNERS_NAME` branch a few lines below, which at least calls `chunk.recover_pk()` before accepting a chunk's payload [4](#0-3) , even though that too only recovers a public key rather than checking authorized-signer-set membership.

The broken equality: the runloop is meant to receive `SignerEvent::MinerMessages` only for chunks that a legitimate `.miners` StackerDB slot owner actually signed and that the local `stacks-node` already validated before firing the event-observer webhook. Instead, in `TryFrom<StackerDBChunksEvent>`, `messages` becomes exactly whatever bytes are in `chunk.data` from the HTTP request body — there is no cryptographic tie between the JSON payload and any StackerDB slot signature at this layer [5](#0-4) .

Root cause: the endpoint's security model relies entirely on network-layer trust — that only the local, paired `stacks-node` (configured via `[[events_observer]] endpoint`) can reach this listener — rather than any in-band authentication. `libsigner`/`stacks-signer` do not implement any authentication for this HTTP listener, and the maintainers explicitly acknowledge this in code: `stacks-signer/src/lib.rs` logs a runtime warning that "the signer is primarily designed for use with a local or subnet network stacks node" and that additional security must be added if exposed further [6](#0-5) . Despite this warning, the shipped reference config `sample/conf/signer/mainnet-signer-conf.toml` sets `endpoint = "0.0.0.0:30000"`, i.e., binds the listener on all interfaces rather than loopback-only [7](#0-6) , while the node-side `mainnet-signer.toml` example only shows `127.0.0.1:30000` for where the node posts to [8](#0-7) . `SignerEventReceiver::bind()` binds exactly the address given with no additional access control layered on top [9](#0-8) .

### Impact Explanation
If the signer's event-receiver port is reachable by a remote party (bound non-loopback, per the shipped `0.0.0.0` sample config), that party can POST an arbitrary, self-crafted `StackerDBChunksEvent` for `MINERS_NAME` and have it accepted verbatim into `SignerEvent::MinerMessages`, which is forwarded via `forward_event()` to the signer runloop exactly as if it came from a legitimate, already-validated miner chunk [10](#0-9) . This is a repeatable, unauthenticated write of forged content (e.g., a fake `BlockProposal`/`BlockPushed`) into the signer's authoritative event stream — matching the "Critical: unauthenticated write to state" category, scoped strictly to the signer's own event-receiver transport.

### Likelihood Explanation
Exploitation requires only that the signer's HTTP listener be reachable from the attacker's network position (no secret, no peer identity, no privileged role needed) — precisely the condition produced by the wildcard-bind sample configuration shipped in this repo (`sample/conf/signer/mainnet-signer-conf.toml`). Where the listener is correctly restricted to loopback/a private subnet (as the runtime warning in `stacks-signer/src/lib.rs` recommends), this is not remotely reachable. The vulnerability is therefore real and code-level (total absence of any in-band authentication or chunk-signature check in the miner branch), but its remote reachability is conditioned on deployment/bind configuration that the project's own shipped sample and code comment both flag as risky when relaxed from loopback-only.

### Recommendation
Add an explicit shared-secret or HMAC check on POSTs to the `SignerEventReceiver` (mirroring the `auth_token`/`auth_password` already used elsewhere) so the receiver authenticates the sender regardless of bind address, and/or default the listener to loopback-only with an explicit opt-in flag (plus a hard warning/refusal) to bind non-loopback. Additionally, in `TryFrom<StackerDBChunksEvent>`'s `MINERS_NAME` branch, verify `chunk.recover_pk()` (or equivalent signature/slot-ownership check) before accepting `chunk.data`, matching the stronger handling already present in the `SIGNERS_NAME` branch.

### Proof of Concept
```rust
// libsigner/src/events.rs (add near existing tests using SignerEventReceiver)
#[test]
fn test_forged_miner_message_unauthenticated() {
    let ev = SignerEventReceiver::<SignerMessage>::new(false);
    let (res_send, _res_recv) = channel();
    let mut signer = Signer::new(SimpleRunLoop::new(1), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:31999".parse().unwrap();
    let running_signer = signer.spawn(endpoint).unwrap();

    // Attacker: craft an unsigned StackerDBChunksEvent for MINERS_NAME
    let forged_json = r#"{
        "contract_id": {"issuer": [26,[0;20]], "name": "miners"},
        "modified_slots": [{
            "slot_id": 0, "slot_version": 1,
            "data": "<attacker-controlled hex/bytes decoding to a fake BlockPushed/BlockProposal>",
            "sig": "00", "data_hash": "00"
        }]
    }"#;
    let body = forged_json.as_bytes();
    let req = format!(
        "POST /stackerdb_chunks HTTP/1.1\r\nHost: {endpoint}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let mut sock = TcpStream::connect(endpoint).unwrap();
    sock.write_all(req.as_bytes()).unwrap();
    sock.write_all(body).unwrap();

    let accepted_events = running_signer.stop().unwrap();
    // Assertion: SignerEvent::MinerMessages contains the attacker's forged payload
    // even though no legitimate StackerDB slot owner signed it.
    assert!(matches!(accepted_events[0], SignerEvent::MinerMessages(ref v) if !v.is_empty()));
}
```
This targets `TryFrom<StackerDBChunksEvent>::try_from` at `libsigner/src/events.rs:549-567`, where `T::consensus_deserialize(&mut chunk.data.as_slice())` is called with no prior signature check.

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

**File:** libsigner/src/events.rs (L469-490)
```rust
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

**File:** libsigner/src/events.rs (L549-567)
```rust
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
```

**File:** libsigner/src/events.rs (L596-612)
```rust
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
```

**File:** stacks-signer/src/lib.rs (L124-132)
```rust
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/mainnet-signer.toml (L26-28)
```text
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```
