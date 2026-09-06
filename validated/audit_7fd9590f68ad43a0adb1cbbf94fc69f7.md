This confirms the design: the signer's `SignerEventReceiver` binds an unauthenticated `HttpServer` (`libsigner/src/events.rs`) that dispatches `/proposal_response` POST bodies straight into `SignerEvent::BlockValidationResponse` with zero caller verification (no token, no source-IP check, no HMAC). The sample configs even show `endpoint = "0.0.0.0:30000"` for the signer's listener, meaning it is explicitly documented/configured to bind on all interfaces, i.e., remotely reachable by design in that reference config.

### Title
Unauthenticated `/proposal_response` HTTP endpoint lets any TCP peer forge `BlockValidateResponse` events into the signer runloop - (File: libsigner/src/events.rs)

### Summary
The signer's event-receiving `HttpServer` accepts unauthenticated POST requests on `/proposal_response` and deserializes the body directly into a `SignerEvent::BlockValidationResponse` without any check that the sender is the local, trusted `stacks-node`. Because the sample/reference signer configuration binds this listener to `0.0.0.0`, any remote TCP peer that can reach the port can inject fabricated `BlockValidateOk`/`BlockValidateReject` payloads into the signer's runloop.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` accepts any HTTP POST to `/proposal_response` and calls `process_event::<T, BlockValidateResponse>(request)`, which only reads the body and calls `serde_json::from_slice` — there is no signature, token, or peer-identity check anywhere in this function. [1](#0-0) [2](#0-1) 

The deserialized value is converted `.try_into()` into `SignerEvent::BlockValidationResponse(BlockValidateResponse)` and forwarded unchanged to the runloop channel via `forward_event`. [3](#0-2) 

The runloop's `process_event` in `stacks-signer/src/v0/signer.rs` treats any `SignerEvent::BlockValidationResponse` as parity-exempt (`None` parity, always processed) and dispatches straight to `handle_event_match` → `handle_block_validate_response`. [4](#0-3) [5](#0-4) 

`handle_block_validate_response` then unconditionally branches on `Ok`/`Reject` and calls `handle_block_validate_ok` or `handle_block_validate_reject`. [6](#0-5) 

The broken equality: nothing in the transport path enforces that the `BlockValidateResponse` acted upon by the signer was actually produced by the paired local node's validation subsystem for a block this signer submitted — the code trusts the HTTP body's `signer_signature_hash` field and payload contents as-is. The documented reference config even exposes this on all interfaces: `endpoint = "0.0.0.0:30000"` in `sample/conf/signer/mainnet-signer-conf.toml`, with the node side matching on `endpoint = "127.0.0.1:30000"` — the binding address is fully attacker/operator-controlled and the sample explicitly uses a wildcard bind. [7](#0-6) 

An `auth_password`/`auth_token` pair exists, but it is documented purely for the **signer → node** RPC direction (the signer calling into the node's block-proposal endpoint), not for the **node → signer** event-POST direction that `SignerEventReceiver` serves; nothing in `next_event`/`process_event` reads or checks any such secret on incoming POSTs. [8](#0-7) 

### Impact Explanation
Any TCP peer that can reach the signer's bound `/proposal_response` port can post a crafted `BlockValidateResponse::Ok` or `::Reject` body for an arbitrary `signer_signature_hash`. This is dispatched into `handle_block_validate_ok`/`handle_block_validate_reject`, which mutate `signer_db` state (`insert_block`, `mark_pre_committed`/`mark_locally_rejected`) and can trigger `send_block_pre_commit`/broadcast of rejection over StackerDB — an unauthenticated write into the signer's local validation-response stream and, transitively, into gossip the signer emits to peers. This matches the "unauthenticated/unauthorized write to state or StackerDB" Critical category, scoped strictly to the transport-delivery step as the question specifies (the signer's downstream trust decisions on the content are out of scope here, but the delivery itself is unauthenticated by design at this layer). [9](#0-8) 

### Likelihood Explanation
No privileged role, secret, or local access is required — only network reachability to the signer's listening port. Reachability is entirely a function of the `endpoint` bind address chosen by the operator; the shipped reference configuration for the signer binary explicitly uses `0.0.0.0:30000`, i.e., "listen on all interfaces," making this remotely exploitable out-of-the-box for anyone following that sample config. Repeatable per HTTP POST, at negligible attacker cost.

### Recommendation
Add authentication to the node→signer event-POST direction: require the node to send a shared secret/HMAC (analogous to `auth_token`) on every `/proposal_response`, `/stackerdb_chunks`, `/new_burn_block`, `/new_block` POST, and have `SignerEventReceiver`/`process_event` verify it before deserializing/forwarding. At minimum, default and strongly recommend binding the signer's `endpoint` to `127.0.0.1` rather than `0.0.0.0` in all sample configs, and document that a wildcard bind is unsafe without an additional authentication layer or firewalling.

### Proof of Concept
Rust test (in `libsigner` or `stacks-signer` test harness):
1. Construct a `SignerEventReceiver<SignerMessage>`, call `bind(SocketAddr::from(([0,0,0,0], 0)))` to get an ephemeral port, and register a consumer channel via `add_consumer`.
2. Spawn `next_event`/`main_loop` in a thread.
3. From a separate `TcpStream::connect` to the bound address (simulating an arbitrary remote peer, not the node), send a raw HTTP request:
   `POST /proposal_response HTTP/1.1\r\nHost: ...\r\nContent-Length: N\r\n\r\n{"result":"Rejected",...crafted BlockValidateReject JSON...}`
4. Assert the consumer channel receives `SignerEvent::BlockValidationResponse(BlockValidateResponse::Reject(_))` with the attacker-chosen `signer_signature_hash`, with no error/rejection at any layer — demonstrating unauthenticated delivery through `process_event::<T, BlockValidateResponse>` in `libsigner/src/events.rs`.

### Citations

**File:** libsigner/src/events.rs (L436-441)
```rust
            debug!("Processing {} event", request.url());
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
```

**File:** libsigner/src/events.rs (L466-490)
```rust
    /// Forward an event
    /// Return true on success; false on error.
    /// Returning false terminates the event receiver.
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

**File:** stacks-signer/src/v0/signer.rs (L371-383)
```rust
        let event_parity = match event {
            // Block proposal events do have reward cycles, but each proposal has its own cycle,
            //  and the vec could be heterogeneous, so, don't differentiate.
            Some(SignerEvent::BlockValidationResponse(_))
            | Some(SignerEvent::MinerMessages(..))
            | Some(SignerEvent::NewBurnBlock { .. })
            | Some(SignerEvent::NewBlock { .. })
            | Some(SignerEvent::StatusCheck)
            | None => None,
            Some(SignerEvent::SignerMessages { signer_set, .. }) => {
                Some(u64::from(*signer_set) % 2)
            }
        };
```

**File:** stacks-signer/src/v0/signer.rs (L510-518)
```rust
        match event {
            SignerEvent::BlockValidationResponse(block_validate_response) => {
                debug!("{self}: Received a block proposal result from the stacks node...");
                self.handle_block_validate_response(
                    stacks_client,
                    block_validate_response,
                    sortition_state,
                )
            }
```

**File:** stacks-signer/src/v0/signer.rs (L1946-1975)
```rust
        if let Some(block_rejection) =
            self.check_block_against_signer_db_state(stacks_client, &block_info.block)
        {
            // The signer db state has changed. We no longer view this block as valid. Override the validation response.
            if let Err(e) = block_info.mark_locally_rejected() {
                if !block_info.has_reached_consensus() {
                    warn!("{self}: Failed to mark block as locally rejected: {e:?}");
                }
            };
            self.signer_db
                .insert_block(&block_info)
                .unwrap_or_else(|e| self.handle_insert_block_error(e));
            self.handle_block_rejection(&block_rejection, sortition_state);
            self.send_block_response(&block_info.block, block_rejection.into());
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
```

**File:** stacks-signer/src/v0/signer.rs (L2053-2071)
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
        };
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L45-50)
```text
# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
