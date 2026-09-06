### Title
Unauthenticated event-observer HTTP endpoint allows forged `SignerEvent` injection into signer runloop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches on the raw HTTP request path alone (`/stackerdb_chunks`, `/new_block`, `/proposal_response`, `/new_burn_block`) with no TLS, shared secret, or source-address check before decoding the JSON body and converting it into a `SignerEvent`. Any TCP peer that can reach the bound listener socket can POST a crafted body and have it accepted as if it came from the configured Stacks node's event dispatcher.

### Finding Description
`next_event` reads the raw HTTP request from the `tiny_http::Server`, checks only `request.method()` and `request.url()`, and immediately calls `process_event::<T, E>(request)` for the matching path [1](#0-0) . `process_event` reads the body, JSON-deserializes it into the target type `E` (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent`), and converts it via `TryInto<SignerEvent<T>>` with no signature, HMAC, or peer-identity check anywhere in this path [2](#0-1) .

For the `/stackerdb_chunks` route in particular, `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` branches on `event.contract_id`: for the `.miners` boot contract it deserializes each `chunk.data` directly into `T` and emits `SignerEvent::MinerMessages` with **no signature/pubkey check at all** [3](#0-2) . For the `signers-X-Y` contracts it does call `chunk.recover_pk()`, but that only recovers *a* public key from whatever signature bytes are in the attacker-supplied chunk — it is not validated against the actual current signer set at this layer [4](#0-3) .

`bind()` simply opens an `HttpServer` on the given `SocketAddr` with no auth wiring [5](#0-4) , and `EventReceiver` interface offers no hook for authenticating the sender before `forward_event` hands the event to the runloop [6](#0-5) . Thus the equality "message delivered to runloop == message the configured node sent" is broken by construction: the transport layer performs zero sender authentication, and the only downstream check (`recover_pk`) validates an attacker-controlled signature blob, not identity against a known key set.

I could not fully verify from the index what network interface the signer's event listener defaults to bind on (i.e., whether stock deployment configs restrict it to loopback via `stacks-signer/src/config.rs`'s `endpoint` field), so whether this is remotely reachable depends on operator deployment/firewalling, which is outside what I can confirm from the available file contents.

### Impact Explanation
If the listener is reachable from the attacker's network position (any interface not restricted to loopback/firewalled), a single unauthenticated POST to `/stackerdb_chunks` with a fabricated miner contract chunk yields `Ok(SignerEvent::MinerMessages(..))` fed straight into the signer's decision channel, and `/new_burn_block`, `/new_block`, `/proposal_response` are equally forgeable. This matches the "unauthenticated/unauthorized write to state" style of impact at the transport/injection level — repeatable per message, no privileged role or secret needed, only network reachability to the port. Whether this rises to the "Critical" category the prompt describes depends entirely on real-world reachability of the port, which is a deployment/binding question this file's code does not settle.

### Likelihood Explanation
No preconditions beyond TCP reachability to the signer's event-listener port; no secret, key, or role is required to construct the HTTP POST bodies. Likelihood is high if the endpoint is exposed beyond localhost/a private admin network; it is unexploitable if operators bind it only to loopback/an internal interface, which is the typical deployment guidance for event-observer style endpoints in this codebase family. This distinction (default bind address / recommended deployment topology) is not resolved within `libsigner/src/events.rs` itself.

### Recommendation
Add a transport-level authentication gate to `SignerEventReceiver` before dispatch — e.g., a shared bearer token/HMAC header checked in `next_event` prior to calling `process_event`, and/or enforce TLS with client-cert pinning, and/or restrict `bind()` to loopback with the node reaching it only via a local trusted proxy. At minimum, document and default the endpoint to bind only on loopback, and reject requests lacking the expected authenticator regardless of path.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) — conceptual PoC
#[test]
fn unauthenticated_stackerdb_chunk_injection() {
    let mut receiver: SignerEventReceiver<v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // "attacker" thread with no relation to the configured node
    std::thread::spawn(move || {
        let mut stream = TcpStream::connect(addr).unwrap();
        let body = serde_json::to_string(&forged_stackerdb_chunks_event_for_miners()).unwrap();
        let req = format!(
            "POST /stackerdb_chunks HTTP/1.1\r\nHost: {addr}\r\nContent-Length: {}\r\n\r\n{}",
            body.len(), body
        );
        stream.write_all(req.as_bytes()).unwrap();
    });

    let event = receiver.next_event().unwrap();
    assert!(matches!(event, SignerEvent::MinerMessages(_))); // succeeds despite unauthenticated sender
}
```
This confirms `next_event` accepts and converts an arbitrary, unauthenticated POST into a `SignerEvent` that would be forwarded via `forward_event` to the runloop. Full end-to-end reachability/severity confirmation requires checking the deployed bind address (`stacks-signer` config's `endpoint`), which was not fully resolvable from the indexed file contents available to me.

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
