Confirmed vulnerability: the signer's `SignerEventReceiver` HTTP listener (`libsigner/src/events.rs`) accepts and processes `BlockValidationResponse` events from any TCP connection with no origin/Host validation or authentication, and `handle_block_validate_ok`/`handle_block_validate_response` (`stacks-signer/src/v0/signer.rs`) trust that verdict to advance a block toward `mark_pre_committed`/signature.

### Title
Unauthenticated signer event listener accepts forged `BlockValidationResponse`/`StackerDBChunksEvent` from any TCP peer, enabling remote pre-commit/signature steering - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::bind`/`next_event` (`libsigner/src/events.rs:404-459`) opens a plain `tiny_http` HTTP server on the signer's configured `endpoint` (sample configs bind `0.0.0.0:30000`, i.e. all interfaces [1](#0-0) [2](#0-1) ) and, for any inbound connection, dispatches `POST /proposal_response`, `/stackerdb_chunks`, `/new_burn_block`, `/new_block` bodies straight into `process_event`, which only checks that the JSON deserializes — there is no Host header check, no Origin check, no shared secret, no TLS client-cert, and no verification that the request came from the loopback stacks-node [3](#0-2) [4](#0-3) . This is the same bug class as the MCP report: a network listener that skips the "is this connection from a source I trust" check that its sibling transports (the P2P protocol's signed messages, or StackerDB chunk writes, which are cryptographically authenticated) perform.

### Finding Description
Every other write path into signer/StackerDB state in this repo is gated by a `MessageSignature` check against a known public key — e.g. `SlotMetadata::verify`/`try_replace_chunk` [5](#0-4) , `validate_received_chunk` [6](#0-5) , and P2P handshake verification [7](#0-6) . The signer's local event-ingestion HTTP endpoint breaks that equality: it is the one inbound channel that accepts and acts on unauthenticated, unsigned JSON. `handle_block_validate_response` → `handle_block_validate_ok` trusts the `BlockValidateOk` verdict it receives from this channel to store the block as valid and call `mark_pre_committed`, which starts the pre-commit/signature countdown [8](#0-7) [9](#0-8) . If this listener is reachable by anyone other than the paired stacks-node (bound to `0.0.0.0`, exposed via port-forwarding/misconfigured firewall/container networking, or reachable from a compromised co-located process), an attacker can POST a forged `/proposal_response` body claiming the node validated an arbitrary block as OK, steering that signer's pre-commit/signature machinery for a block the real node never validated. `StackerDBChunksEvent` bodies pushed to `/stackerdb_chunks` still pass through signature-aware downstream consumers (`chunk.recover_pk()`/`signer_pubkey.verify`), so that specific channel is not fully spoofable end-to-end, but `/proposal_response` and `/new_burn_block`/`/new_block` events carry no equivalent per-message signature check at the transport boundary, so trust is placed entirely on network topology.

### Impact Explanation
This maps to the "auth-gate that fails open" / non-canonical-data-served analog called out in scope: a supposedly node-only local channel accepts data as if it came from the paired stacks-node, with no cryptographic or origin check, and that data feeds directly into consensus-adjacent signer decisions (`mark_pre_committed`, pre-commit broadcast, eventual signature). Where this listener is reachable (misconfigured bind, container/LAN exposure), it is a High-severity "steering a node off the tip via false inventory"-class issue: an attacker-controlled peer can inject fabricated validation verdicts into a signer's state machine.

### Likelihood Explanation
Exploitability depends entirely on network exposure of the signer's event-listener port. Deployment guidance and default sample configs bind to `127.0.0.1`-style addresses in some templates, but the mainnet signer sample config explicitly shows `endpoint = "0.0.0.0:30000"` [1](#0-0) , and nothing in `SignerEventReceiver` itself enforces loopback-only or authenticated access — the protection is purely operator-provided firewalling, not code-enforced. This mirrors the MCP advisory's own framing: the defect is real and remotely exploitable whenever the operator doesn't additionally lock down the port, and the SDK/library itself provides no gate.

### Recommendation
Add a mandatory authentication/origin check to `SignerEventReceiver` analogous to `TransportSecuritySettings` in the referenced advisory: require a shared secret/HMAC or mutual-auth check on incoming event POSTs (the node already has an `auth_token`/`auth_password` pairing used for the RPC direction — extend an equivalent check to this direction), and/or bind by default to loopback only and reject requests whose `Host`/source address doesn't match an allow-list, independent of user-supplied firewall configuration.

### Proof of Concept
1. Configure a stacks-signer per `sample/conf/signer/mainnet-signer-conf.toml` with `endpoint = "0.0.0.0:30000"`.
2. From a remote host that can reach port 30000 (e.g. due to a misconfigured firewall/NAT), send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"Ok": {"signer_signature_hash": "<hash-of-victim-tracked-block>", "cost": {...}, "size": 0, "validation_time_ms": 0}}
```
3. `SignerEventReceiver::next_event` accepts this without any credential check and forwards it as `SignerEvent::BlockValidationResponse` [10](#0-9) .
4. `handle_block_validate_response`/`handle_block_validate_ok` process it as if the paired stacks-node had validated the block, potentially advancing it to `PreCommitted` [9](#0-8) .

### Citations

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

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

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
```rust
        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```

**File:** stackslib/src/net/chat.rs (L1058-1071)
```rust
        match self.connection.get_public_key() {
            None => {
                // if we don't yet have a public key for this node, verify the message.
                // if it's improperly signed, it's probably a poorly-timed re-key request (but either way the message should be rejected)
                message
                    .verify_secp256k1(&handshake_data.node_public_key)
                    .map_err(|_e| {
                        debug!(
                            "{:?}: invalid handshake: not signed with given public key",
                            &self
                        );
                        net_error::InvalidMessage
                    })?;
            }
```

**File:** stacks-signer/src/v0/signer.rs (L1960-1976)
```rust
        } else {
            if let Err(e) = block_info.mark_pre_committed() {
                // The block may have reached enough signatures before we validated the block so should fail to mark pre-committed
                // but still call to make sure the timestamps and validity are updated correctly.
                if !block_info.has_reached_consensus()
                    && block_info.state != BlockState::LocallyAccepted
                {
                    warn!("{self}: Failed to mark block as approved: {e:?}",);
                    return;
                }
            }

            self.signer_db
                .insert_block(&block_info)
                .unwrap_or_else(|e| self.handle_insert_block_error(e));
            self.send_block_pre_commit(signer_signature_hash.clone());
            // have to save the signature _after_ the block info
```

**File:** stacks-signer/src/v0/signer.rs (L2053-2070)
```rust
    /// Handle the block validate response returned from our prior calls to submit a block for validation
    fn handle_block_validate_response(
        &mut self,
        stacks_client: &StacksClient,
        block_validate_response: &BlockValidateResponse,
        sortition_state: &mut Option<SortitionsView>,
    ) {
        info!("{self}: Received a block validate response: {block_validate_response:?}");
        match block_validate_response {
            BlockValidateResponse::Ok(block_validate_ok) => {
                crate::monitoring::actions::record_block_validation_latency(
                    block_validate_ok.validation_time_ms,
                );
                self.handle_block_validate_ok(stacks_client, block_validate_ok, sortition_state);
            }
            BlockValidateResponse::Reject(block_validate_reject) => {
                self.handle_block_validate_reject(block_validate_reject, sortition_state);
            }
```
