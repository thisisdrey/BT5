### Title
Unauthenticated, Unbounded Body Read in `libsigner` Signer Event Receiver Enables Remote Memory-Exhaustion DoS - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event()` and its helper `process_event()` in `libsigner/src/events.rs` accept HTTP POST requests on a `tiny_http` listener and read the entire request body into a `String` via `request.as_reader().read_to_string(&mut body)` before any authentication, size limit, or content validation is performed. This mirrors the Argo Workflows webhook flaw exactly: a request body is fully buffered in memory ahead of any check that would reject or bound it.

### Finding Description
The signer runs an HTTP server (`SignerEventReceiver::bind`, using `tiny_http::Server`) to receive events pushed by a paired Stacks node: `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`. [1](#0-0) 

Dispatch in `next_event()` routes based only on `request.url()` and `request.method()` — there is no signature check, shared-secret check, or peer-identity check of any kind before the body is read: [2](#0-1) 

The actual body consumption happens in `process_event()`:
```rust
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) {
    ...
}
```
No `Content-Length` cap, no `MAX_HTTP_HEADER_LEN`-style bound, and no `BoundReader`/`MAX_MESSAGE_LEN` check (both of which exist elsewhere in this codebase, e.g. `stackslib/src/net/connection.rs` and `stackslib/src/net/httpcore.rs`) is applied to the body before or during this read. [3](#0-2) 

Only after the entire body has been fully materialized in memory does the code attempt JSON deserialization (`serde_json::from_slice`) and any subsequent structural/signature validation of the deserialized event (e.g., `StackerDBChunksEvent` -> chunk signature checks in `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`). [4](#0-3) [5](#0-4) 

This breaks the same invariant flagged in the Argo report: *bytes read into memory* should not exceed a bound imposed *before* any authentication/verification occurs. Elsewhere in the same repo, the equivalent pattern is defended against — P2P messages are capped by `MAX_MESSAGE_LEN` before buffering (`stackslib/src/net/connection.rs::consume_preamble`) [6](#0-5) , and HTTP requests handled by the node's RPC server are likewise capped via `payload_len()`/`MAX_MESSAGE_LEN` checks before the body is fully buffered (`stackslib/src/net/httpcore.rs`) [7](#0-6) . The `libsigner` event receiver has no analogous bound.

I was unable to fully confirm, from the indexed code alone, the exact default bind address (`0.0.0.0` vs. loopback-only) configured for the signer's event endpoint (`stacks-signer/src/config.rs` references `endpoint` extensively, but I could not verify the default value or whether operators are required to firewall it). This affects how directly "remote/unauthenticated" the exposure is in a default deployment, and should be verified against the actual config defaults and deployment documentation.

### Impact Explanation
Any TCP client that can reach the signer's event-receiver port can send a POST to any of the recognized paths (or even to `/shutdown`, which is unauthenticated too) with an arbitrarily large body and force the process to allocate memory proportional to the attacker-supplied payload size before any validation happens. Repeated or parallel requests can exhaust signer process memory, causing an OOM kill/crash of the signer — a `stacks-signer` process outage impacts that signer's participation in block/stacking signing rounds. This matches the "Critical: remote crash/unauthenticated DoS from few messages" bucket in the grading rubric, scoped to signer availability (not consensus-state corruption).

### Likelihood Explanation
Likelihood is high if the event endpoint is reachable beyond localhost, since exploitation requires no credentials, no valid StackerDB signature, and no protocol handshake — just a well-formed HTTP POST with a large `Content-Length`/body to a known, unauthenticated path. Likelihood is contingent on the deployed bind address; if signer operators universally bind this listener to `127.0.0.1`, remote exploitability is eliminated and the issue becomes local-only (still a DoS from any co-located unprivileged process). This detail could not be conclusively resolved from the index and should be checked directly against `stacks-signer/src/config.rs` and default config files.

### Recommendation
- Wrap the request reader in a bounded reader (similar to `stacks_common::util::retry::BoundReader`, already used elsewhere in this codebase) and reject requests whose `Content-Length` exceeds a sane maximum (e.g., aligned with `BLOCK_RESPONSE_DATA_MAX_SIZE` or `MAX_MESSAGE_LEN`) before calling `read_to_string`.
- Enforce this size check prior to reading any bytes, not just prior to JSON deserialization.
- Consider requiring the event push endpoint to be bound to loopback by default, or add a shared-secret/HMAC check on inbound events (as is already done for the block-proposal and fast-call RPC endpoints via `Authorization` headers in `stackslib/src/net/api/postblock_v3.rs` and `stackslib/src/net/api/fastcallreadonly.rs`) before consuming the body.

### Proof of Concept
1. Start `stacks-signer` such that its event receiver is bound to the configured `endpoint`.
2. From a network-reachable client, open a TCP connection and send:
   ```
   POST /new_burn_block HTTP/1.1
   Host: <signer-host>
   Content-Type: application/json
   Content-Length: 2000000000
   Connection: close

   <stream ~2GB of arbitrary bytes>
   ```
3. Observe `process_event()` invoking `request.as_reader().read_to_string(&mut body)`, which allocates memory proportional to the streamed payload before any authentication or JSON validation occurs.
4. Monitor signer process RSS; repeated/parallel requests can drive the process to OOM termination.

### Citations

**File:** libsigner/src/events.rs (L401-458)
```rust
    /// Start listening on the given socket address.
    /// Returns the address that was bound.
    /// Errors out if bind(2) fails
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

**File:** stackslib/src/net/connection.rs (L704-724)
```rust
    fn consume_preamble(
        &mut self,
        protocol: &mut P,
        bytes: &[u8],
    ) -> Result<(Option<P::Preamble>, usize), net_error> {
        let bytes_consumed = self.buffer_preamble_bytes(protocol, bytes);
        let preamble_opt = match protocol.read_preamble(&self.buf) {
            Ok((preamble, preamble_len)) => {
                assert!((preamble_len as u32) < MAX_MESSAGE_LEN); // enforced by protocol family

                test_debug!("Got preamble {:?} of {} bytes", &preamble, preamble_len);

                if let Some(payload_len) = protocol.payload_len(&preamble) {
                    if (payload_len as u32) >= MAX_MESSAGE_LEN {
                        // message would be too big
                        return Err(net_error::DeserializeError(format!(
                            "Preamble payload length {} is too big",
                            payload_len
                        )));
                    }
                }
```

**File:** stackslib/src/net/httpcore.rs (L1550-1559)
```rust
    fn payload_len(&mut self, preamble: &StacksHttpPreamble) -> Option<usize> {
        match *preamble {
            StacksHttpPreamble::Request(ref http_request_preamble) => {
                Some(http_request_preamble.get_content_length() as usize)
            }
            StacksHttpPreamble::Response(ref http_response_preamble) => http_response_preamble
                .content_length
                .map(|len| len as usize),
        }
    }
```
