## Finding

### Title
Missing Authentication on Signer Event Receiver HTTP Endpoint Enables Unauthenticated Remote Shutdown and Forged Event Injection - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver`, the HTTP server that the `stacks-signer` binds to receive event-dispatcher callbacks from a paired `stacks-node`, performs **no authentication or origin check** on any inbound request. Any network peer that can reach the bound address can POST directly to its dispatch routes and be treated exactly as if it were the trusted, paired node.

### Finding Description
`SignerEventReceiver::bind` opens a bare `tiny_http` server with no credential check: [1](#0-0) 

`next_event` then dispatches purely on URL path, with zero authorization gate — contrast this with the node's own `postblock_proposal` RPC handler, which requires a matching `auth_token`/password header. Here there is no equivalent check at all: [2](#0-1) 

Two concrete, remotely reachable faults follow from this missing authorization:

1. **Unauthenticated shutdown / DoS.** The `/shutdown` route sets the stop signal and returns `EventError::Terminated`, which breaks `main_loop`, permanently killing the event receiver thread from a single unauthenticated request: [3](#0-2) 

2. **Forged event injection without cryptographic proof of origin.** `/stackerdb_chunks` is deserialized and converted into a `SignerEvent` and forwarded to the signer run-loop as if it truly came from the paired node's event dispatcher. For the `miners` boot contract lane specifically, chunk payloads are deserialized directly with no signature/ownership check on the chunk at all (unlike the `signers-X-Y` lane, which does call `chunk.recover_pk()`): [4](#0-3) 

The `/proposal_response`, `/new_burn_block`, and `/new_block` routes have the same problem — any JSON body that deserializes successfully is accepted as a genuine node-originated event and pushed into `forward_event`, with no way for the signer to distinguish a real node callback from an attacker-crafted one: [5](#0-4) 

This is the direct analog of the reported CWE-285/862 class: a component that should require the caller to be a specific authorized party (the paired stacks-node) instead accepts input from anyone able to reach the socket, because the missing-permission-check equivalent (an auth token comparable to `connection_options.auth_token` used for `postblock_proposal`) was never added on this transport.

Sample configuration in this same repository binds the endpoint to all interfaces by default, increasing reachability: [6](#0-5) 

### Impact Explanation
- Immediate, single-request unauthenticated denial-of-service against the signer's event pipeline via `/shutdown` (Critical: "remote crash/unauthenticated DoS from few messages").
- Unauthenticated injection of forged `SignerEvent`s (miner messages, burn-block/new-block notices, and block-validation responses) into the signer's internal state machine, since there is no proof that the caller is the legitimate paired node (Critical: "unauthenticated/unauthorized write to state").

### Likelihood Explanation
High. No credential, token, or peer-address check exists anywhere in the dispatch path (`next_event`), and the documented/sample configuration binds the listener to `0.0.0.0`. Any attacker with network access to the configured port can exploit this without any prerequisite (no key, no prior session, no privileged role).

### Recommendation
Add a shared-secret/token check (mirroring `connection_options.auth_token` used by `postblock_proposal`) that `SignerEventReceiver::next_event` validates on every request before dispatch, rejecting unauthenticated requests with `401`. At minimum, gate the `/shutdown` route behind this check, and default-bind the listener to loopback unless explicitly overridden.

### Proof of Concept
```bash
# Unauthenticated remote shutdown of the signer's event receiver
curl -X POST http://<signer-host>:30000/shutdown -d 'x'

# Unauthenticated forged event injection
curl -X POST http://<signer-host>:30000/new_burn_block \
  -H 'Content-Type: application/json' \
  -d '{"burn_block_height":999999,"burn_block_hash":"...","consensus_hash":"...","parent_burn_block_hash":"..."}'
```
Both requests succeed without any credentials, because `next_event` performs no authorization check before acting on the payload.

### Citations

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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```
