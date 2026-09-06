### Title
Unauthenticated remote shutdown and event injection in the signer's `SignerEventReceiver` HTTP endpoint - (File: libsigner/src/events.rs)

### Summary
The `SignerEventReceiver` that the `stacks-signer` binary uses to receive events pushed by a `stacks-node` (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/shutdown`, `/status`) performs **no authentication whatsoever** on incoming HTTP requests, unlike the analogous node-side RPC endpoints (e.g. `postblock_proposal`, `blockreplay`, `blocksimulate`) which enforce an `authorization` header check and return 401 on mismatch.

### Finding Description
`SignerEventReceiver::next_event` [1](#0-0)  dispatches any HTTP POST reaching the bound socket purely by URL path, with zero credential/token verification:
- `/status` → responds 200 OK.
- `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` → deserialize the body directly into `StackerDBChunksEvent` / `BlockValidateResponse` / `BurnBlockEvent` / `StacksBlockEvent` and forward as a `SignerEvent` to the signer runloop via `forward_event`.
- `/shutdown` → sets `stop_signal` and returns `EventError::Terminated`, which causes `main_loop` to `break` and terminate the receiver thread [2](#0-1) .

No `Authorization`/`auth_password` check exists anywhere in `libsigner` (confirmed via search — no matches for `Authorization`/`auth_password`/`check_auth` in the `libsigner` tree), in contrast to the node-side `[connection_options] auth_token` gate documented for RPC endpoints such as `/v3/block_proposal` [3](#0-2) . The signer's reference configuration binds this receiver to `0.0.0.0:30000` [4](#0-3) , i.e., all interfaces, not loopback-only.

This breaks the intended equality "only the paired stacks-node may deliver events to the signer" — the fault site is the complete absence of a credential check at `SignerEventReceiver::next_event`, analogous in bug-class to the TYPO3 report's failure to gate a privileged-only surface (there it was output-encoding on a backend-only view; here it is missing authentication on an operationally-privileged control/data channel).

Additionally, once a forged `StackerDBChunksEvent` reaches `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`, each embedded chunk's signer identity is derived only via ECDSA public-key *recovery* from the attacker-supplied signature (`chunk.recover_pk()`), which always succeeds for any self-consistent signature/message pair regardless of whether the recovered key belongs to a legitimate registered signer [5](#0-4) . Membership validation against the actual signer set happens only later, in the miner-side listener (`stackerdb_listener.rs`), not in this ingestion path — so the signer process itself accepts and forwards arbitrary attacker-crafted messages as "signer events" to its own runloop.

### Impact Explanation
The `/shutdown` route provides an unauthenticated, single-request remote crash/DoS of the signer's event-receiving thread: any actor with network reachability to the configured endpoint (`0.0.0.0:<port>` per the shipped reference config) can permanently halt the signer's ability to receive new node events (block proposals, burn-block events, stackerdb chunks) with one HTTP POST — matching the "Critical - remote crash/unauthenticated DoS from few messages" impact tier. Beyond DoS, the same lack of authentication allows an unauthenticated party to inject forged `StackerDBChunksEvent`/`BlockValidateResponse`/`BurnBlockEvent` payloads directly into the signer's event pipeline, which is an unauthenticated write into signer-observed state (though what the signer *does* with those events is signer-decision logic, explicitly out of scope for this analog; the injection surface itself is in scope).

### Likelihood Explanation
No secrets, credentials, or prior state are needed — only network reachability to the signer's event port. The reference configuration binds to `0.0.0.0`, i.e. all network interfaces, elevating this from a purely local design assumption to a realistically remote-reachable endpoint whenever operators follow the documented sample config without adding external firewalling. The `/shutdown` payload requires no body and no parsing beyond the URL match, making exploitation trivial.

### Recommendation
Add a shared-secret/token check (mirroring the node's `[connection_options] auth_token`/`Authorization` header pattern used elsewhere in this codebase) to `SignerEventReceiver::next_event` before dispatching to any handler, especially `/shutdown`. Bind the receiver to loopback by default and require explicit opt-in plus authentication to listen on non-loopback interfaces. Independently, validate recovered StackerDB chunk signer public keys against the actual registered signer set before treating a chunk as an authentic signer message, rather than accepting any recoverable signature.

### Proof of Concept
1. Deploy `stacks-signer` with the shipped reference config (`endpoint = "0.0.0.0:30000"`).
2. From any host that can reach port 30000 (no credentials needed):
   ```
   POST /shutdown HTTP/1.1
   Host: <signer-ip>:30000
   Connection: close

   ```
3. The signer's `SignerEventReceiver` sets `stop_signal`, `next_event` returns `EventError::Terminated`, and `main_loop` exits — the signer stops processing all further node events (block proposals, burn blocks, stackerdb chunks) until manually restarted, as shown by the request-handling flow in `libsigner/src/events.rs:413-458` and `libsigner/src/events.rs:282-312`.

### Citations

**File:** libsigner/src/events.rs (L282-312)
```rust
    /// Main loop for the receiver.
    /// Typically, this is started in a separate thread.
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
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

**File:** libsigner/src/events.rs (L596-613)
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
                })
```

**File:** stackslib/src/net/api/tests/postblock_proposal.rs (L83-98)
```rust
    let mut handler =
        postblock_proposal::RPCBlockProposalRequestHandler::new(Some("password".into()));

    // missing authorization header
    let bad_request = http.handle_try_parse_request(
        &mut handler,
        &parsed_preamble.expect_request(),
        &bytes[offset..],
    );
    match bad_request {
        Err(crate::net::Error::Http(crate::net::http::Error::Http(err_code, message))) => {
            assert_eq!(err_code, 401);
            assert_eq!(message, "Unauthorized");
        }
        _ => panic!("expected error"),
    }
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```
