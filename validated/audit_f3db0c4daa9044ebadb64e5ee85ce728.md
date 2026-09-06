This confirms critical impact: `MinerMessages` are dispatched to `handle_event_match` in `stacks-signer/src/v0/signer.rs:571-624`, where `SignerMessage::BlockProposal` is fed directly into `handle_block_proposal` and `SignerMessage::BlockPushed` into `handle_post_block`, with no signature/ownership check performed anywhere in that path — the only place such a check could occur (`chunk.recover_pk()`) is skipped entirely for the `MINERS_NAME` branch in `TryFrom<StackerDBChunksEvent>`.

### Title
Unauthenticated forged `.miners` StackerDB chunk data is accepted as `SignerEvent::MinerMessages` because the miner-slot signature is never verified - (File: libsigner/src/events.rs)

### Summary
`TryFrom<StackerDBChunksEvent>::try_from` in `libsigner/src/events.rs` dispatches on `contract_id.name`: the `SIGNERS_NAME` branch calls `chunk.recover_pk()` to verify the StackerDB slot's signature before accepting a message, but the `MINERS_NAME` branch simply runs `T::consensus_deserialize` on `chunk.data` and pushes the result into `SignerEvent::MinerMessages` with zero signature verification. Any party able to reach the signer's event-receiver HTTP listener can POST a crafted `/stackerdb_chunks` body with `contract_id.name == MINERS_NAME` and an unsigned/garbage-signed chunk, and it will be forwarded as if it were signed by the current `.miners` slot owner.

### Finding Description
In `libsigner/src/events.rs:544-624`, the `MINERS_NAME` branch (lines 549-567) does:
```rust
for chunk in event.modified_slots {
    match T::consensus_deserialize(&mut chunk.data.as_slice()) {
        Ok(msg) => messages.push(msg),
        ...
    }
}
SignerEvent::MinerMessages(messages)
``` [1](#0-0) 
No call to `chunk.recover_pk()` or any other signature check exists on this path, unlike the `SIGNERS_NAME` branch just below it which explicitly does `let Ok(pk) = chunk.recover_pk() else { ...skip... }` before accepting a message [2](#0-1) .

`process_event` (`libsigner/src/events.rs:519-542`) reads the raw HTTP POST body, deserializes it via `serde_json::from_slice` into `StackerDBChunksEvent`, and calls `.try_into()` with no authentication check on the incoming request at all [3](#0-2) . `next_event` routes any POST to `/stackerdb_chunks` straight into `process_event::<T, StackerDBChunksEvent>` [4](#0-3) .

The signer's event-receiver endpoint binds to whatever address is configured — the shipped reference config explicitly sets `endpoint = "0.0.0.0:30000"` (all interfaces) [5](#0-4) , and there is no `auth_password`/`auth_token` check anywhere in `SignerEventReceiver`/`process_event` for incoming requests (that credential is only used for the signer's outbound calls to the node's RPC). Anyone who can open a TCP connection to that port can therefore forge a `MinerMessages` event.

The forged messages are then consumed unconditionally by `stacks-signer/src/v0/signer.rs` `handle_event_match`, which for `SignerEvent::MinerMessages(messages)` matches `SignerMessage::BlockProposal` into `self.handle_block_proposal(...)` and `SignerMessage::BlockPushed` into `self.handle_post_block(...)` with no additional signer-side authentication of the message's origin [6](#0-5) . Unlike the `SignerMessages` branch, which explicitly validates `is_valid_signer(&signer_address)` derived from the `recover_pk()` result before doing anything [7](#0-6) , the `MinerMessages` branch has no equivalent gate because it trusts that `try_from` already verified the miner's ownership — which it does not.

### Impact Explanation
This breaks the OWNERSHIP invariant that every `MinerMessages` entry delivered to the runloop is signed by the current `.miners` slot owner. An attacker who can reach the signer's event-receiver port can inject a forged `BlockProposal` or `BlockPushed` message that gets processed by `handle_block_proposal`/`handle_post_block` as though it came from the legitimate sortition-winning miner. This is an unauthenticated write into a security-relevant event stream that participates in block-signing decisions, matching the "unauthenticated/unauthorized write to state" Critical category. It is fully repeatable per POST request.

### Likelihood Explanation
The event-receiver endpoint is reachable at whatever address is configured; the project's own reference/sample configuration binds it to `0.0.0.0` [5](#0-4) , and `SignerEventReceiver::bind`/`next_event` perform no authentication of the caller [8](#0-7) . No node RPC secret, peer key, or slot ownership is required — only network reachability to the configured port, matching the "unprivileged remote attacker" threat model.

### Recommendation
In the `MINERS_NAME` branch of `TryFrom<StackerDBChunksEvent>` (`libsigner/src/events.rs:549-567`), call `chunk.recover_pk()` and verify the recovered public key corresponds to the currently-authorized miner (e.g., cross-check against the sortition-winner/miner key for the current tenure) before pushing `msg` into `messages`, mirroring the verification already done in the `SIGNERS_NAME` branch. At minimum, reject/skip chunks whose signature fails to recover, and ideally bind/restrict the event-receiver listener to a trusted interface as defense in depth.

### Proof of Concept
Rust test (added to `libsigner/src/tests/mod.rs`, modeled on `test_simple_signer`):
1. Spawn a `SignerEventReceiver` bound to `127.0.0.1:<port>` and a `SimpleRunLoop` via `Signer::spawn`, as in `test_simple_signer` [9](#0-8) .
2. Build a `StackerDBChunkData::new(slot_id, version, message_bytes)` for a `SignerMessage::BlockProposal(...)`, but instead of calling `chunk.sign(&privk)`, either leave `chunk.sig` as its default/zeroed value or sign with a throwaway key unrelated to any registered miner.
3. Wrap it in `StackerDBChunksEvent { contract_id: NakamotoSigners::make_miners_db_contract_id(...)/MINERS_NAME boot contract, modified_slots: vec![chunk] }`.
4. POST the serialized JSON to `/stackerdb_chunks` over a raw `TcpStream`, exactly as `test_simple_signer` does [10](#0-9) .
5. Assert that `running_signer.stop()` (or the accepted-events channel) contains `SignerEvent::MinerMessages(vec![SignerMessage::BlockProposal(...)])` despite the invalid/garbage signature — proving delivery occurs at `libsigner/src/events.rs:567` without any `recover_pk` gate, in contrast to the `SIGNERS_NAME` path where an equivalent unsigned chunk would be silently dropped at line 596-603.

### Citations

**File:** libsigner/src/events.rs (L404-458)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }

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

**File:** libsigner/src/events.rs (L519-541)
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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** stacks-signer/src/v0/signer.rs (L529-538)
```rust
                for (_slot_id, signer_public_key, message) in messages {
                    let signer_address = StacksAddress::p2pkh(self.mainnet, signer_public_key);
                    if !self.is_valid_signer(&signer_address) {
                        debug!("{self}: Received a message from an unknown signer. Ignoring...";
                            "signer_public_key" => ?signer_public_key,
                            "signer_address" => %signer_address,
                            "message" => ?message,
                        );
                        continue;
                    }
```

**File:** stacks-signer/src/v0/signer.rs (L571-602)
```rust
            SignerEvent::MinerMessages(messages) => {
                debug!(
                    "{self}: Received {} messages from the miner",
                    messages.len();
                );
                for message in messages {
                    match message {
                        SignerMessage::BlockProposal(block_proposal) => {
                            #[cfg(any(test, feature = "testing"))]
                            if self.test_ignore_all_block_proposals(block_proposal) {
                                continue;
                            }
                            #[cfg(any(test, feature = "testing"))]
                            if self.test_insert_block_proposal_without_processing(block_proposal) {
                                continue;
                            }
                            self.handle_block_proposal(
                                stacks_client,
                                sortition_state,
                                block_proposal,
                            );
                        }
                        SignerMessage::BlockPushed(b) => {
                            // This will infinitely loop until the block is acknowledged by the node
                            info!(
                                "{self}: Got block pushed message";
                                "block_id" => %b.block_id(),
                                "block_height" => b.header.chain_length,
                                "signer_signature_hash" => %b.header.signer_signature_hash(),
                            );
                            self.handle_post_block(stacks_client, b);
                        }
```

**File:** libsigner/src/tests/mod.rs (L93-117)
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

```

**File:** libsigner/src/tests/mod.rs (L121-146)
```rust
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
