Based on my investigation, this is a legitimate, in-scope analog. The `libsigner` event receiver's HTTP endpoint has no authentication and blindly trusts whatever is POSTed to it as authentic node data, but the messages within a `StackerDBChunksEvent` for the `.signers-*` boot contracts are only trusted for their *type* (via `chunk.recover_pk()`), not verified against the actual registered signer set membership at that layer — the trust boundary is that only the node is supposed to be able to reach this port.

### Title
Unauthenticated HTTP event-injection endpoint allows forged signer/StackerDB events to reach the `stacks-signer` process - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` opens a plain HTTP server via `HttpServer::http(listener)` and dispatches any POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` straight into `process_event`, with no check of source, no shared secret, and no signature over the transport itself. [1](#0-0) 

### Finding Description
The signer's event listener is designed to receive events exclusively from its paired `stacks-node` (configured via `events_observer.endpoint`), and the documentation for `auth_token`/`auth_password` describes a shared secret that is supposed to secure this channel — but that secret is only used for the *node's* HTTP endpoints (`/v3/block_proposal`, `/v2/blocks?broadcast=1`), not for the signer's own inbound listener. The `SignerEventReceiver::bind`/`next_event` implementation never validates any `Authorization` header or token on incoming requests; it just parses the JSON body and forwards it. [2](#0-1) [3](#0-2) 

Any network peer that can reach the signer's bind address (sample configs default to `0.0.0.0:30000`, i.e. all interfaces) can therefore POST a crafted `StackerDBChunksEvent` JSON body. For the `.signers-*` boot contract, the payload is converted via `TryFrom<StackerDBChunksEvent> for SignerEvent` in `libsigner/src/events.rs`, which only checks that `chunk.recover_pk()` succeeds (i.e., that *some* valid signature/pubkey can be recovered from *any* attacker-chosen key over the attacker-chosen data) — it does not check that the recovered key belongs to the actual registered signer set, nor that the "chunk" ever passed through the real StackerDB write-path validation (`try_replace_chunk` / `validate_received_chunk` in `stackslib/src/net/stackerdb/`). [4](#0-3)  That real validation logic runs only on the *node's* peer-to-peer/HTTP StackerDB write path; it is completely bypassed when an attacker talks directly to the signer's event port.

This breaks the intended equality "chunk delivered to the signer == chunk that was actually accepted and stored by the node's StackerDB, signed by a slot's real owner". An attacker with network reachability to the signer's listening port — no node credentials, no StackerDB write access, no signer key — can forge arbitrary `SignerEvent::SignerMessages`, `SignerEvent::MinerMessages`, `SignerEvent::NewBlock`, `SignerEvent::NewBurnBlock`, or `SignerEvent::BlockValidationResponse` events and inject them straight into the signer's runloop.

### Impact Explanation
This is an unauthenticated, unprivileged write into the signer's internal event stream, which downstream consumers (e.g. `StackerDBListener` in `stacks-node/src/nakamoto_node/stackerdb_listener.rs`) treat as legitimate signer traffic once basic signature-recoverability and payload-type checks pass. Depending on deployment (`auth_token`/network isolation not enforced by the signer's own listener), this can be leveraged to inject spurious burn-block/new-block notifications or signer messages that influence signer-side bookkeeping/state without the attacker possessing any real signer/miner key material for the corresponding slot — an unauthenticated write to signer-observed state that should require possession of the node's private channel. This aligns with "Critical: unauthenticated/unauthorized write to state" per the scoring rubric, since it is a remote, unauthenticated network-write vulnerability reachable via a bare HTTP POST with no prior handshake.

### Likelihood Explanation
Likelihood is highly deployment-dependent: exploitability requires the signer's event-receiver port to be reachable by the attacker (the sample configs bind `0.0.0.0`, i.e., all interfaces, and the security guidance in `stacks-signer/src/lib.rs` explicitly warns operators about exposing this channel to untrusted networks), and correct deployment/firewalling would mitigate it. However, the code itself provides zero authentication at the transport layer, so any operator who follows the default sample configuration (`endpoint = "0.0.0.0:30000"`) without external firewalling is exposed to remote, unauthenticated event injection.

### Recommendation
Add an authentication check (e.g., a shared bearer token matching the node's `auth_token`, or restricting binds to loopback/mTLS) inside `SignerEventReceiver::next_event` in `libsigner/src/events.rs` before dispatching to `process_event`, so that only the paired node can post events. Additionally, harden `TryFrom<StackerDBChunksEvent> for SignerEvent` to cross-check the recovered public key against the actual current signer set for the target contract before constructing `SignerEvent::SignerMessages`, rather than accepting any recoverable signature.

### Proof of Concept
1. Start a `stacks-signer` with the sample config (`endpoint = "0.0.0.0:30000"`), as documented in `docs/signing.md` and `sample/conf/signer/mainnet-signer-conf.toml`.
2. From any host that can reach `30000/tcp` on the signer's address, craft a `StackerDBChunksEvent` JSON body targeting the `.signers-0-X` boot contract, with a `StackerDBChunkData` chunk containing arbitrary attacker-chosen `data` self-signed with an attacker-generated key (any key works — `recover_pk()` only needs a recoverable signature, not a registered one).
3. POST this JSON to `http://<signer-host>:30000/stackerdb_chunks` with `Content-Type: application/json`.
4. Observe (as demonstrated by the test harness pattern in `libsigner/src/tests/mod.rs`, which performs exactly this POST flow) that the signer's `SignerEventReceiver` accepts and forwards the event into `SignerEvent::SignerMessages` without ever contacting the real stacks-node or validating the signer's slot ownership. [5](#0-4)

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

**File:** libsigner/src/events.rs (L580-620)
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
        } else {
```

**File:** stackslib/src/config/mod.rs (L3802-3816)
```rust
    /// HTTP auth password to use when communicating with stacks-signer binary.
    ///
    /// This token is used in the `Authorization` header for certain requests.
    /// Primarily, it secures the communication channel between this node and a
    /// connected `stacks-signer` instance.
    ///
    /// It is also used to authenticate requests to `/v2/blocks?broadcast=1`.
    /// ---
    /// @default: `None` (authentication disabled for relevant endpoints)
    /// @notes:
    ///   - This field **must** be configured if the node needs to receive
    ///     block proposals from a configured `stacks-signer` [[events_observer]]
    ///     via the `/v3/block_proposal` endpoint.
    ///   - The value must match the token configured on the signer.
    pub auth_token: Option<String>,
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
