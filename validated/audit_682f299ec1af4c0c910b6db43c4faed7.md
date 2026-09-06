Confirmed: `libsigner/src/events.rs` contains zero references to `Authorization`/`auth_token` anywhere in the crate — the `SignerEventReceiver`'s HTTP listener has no authentication mechanism at all.

### Title
Unauthenticated event-injection endpoint on stacks-signer's `SignerEventReceiver` accepts forged StackerDB/block/burn-block events from any network peer - (File: libsigner/src/events.rs)

### Summary
The `stacks-signer` process runs a plaintext HTTP server (`SignerEventReceiver`) that is meant to receive event pushes only from its paired Stacks node's event-dispatcher. The handler performs no authentication, source-IP check, or shared-secret validation before accepting and forwarding `POST /stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` payloads into the signer's runloop as trusted `SignerEvent`s.

### Finding Description
`SignerEventReceiver::next_event` dispatches incoming HTTP requests purely by URL path and method, with no credential check of any kind: [1](#0-0) 

The dispatched handler, `process_event`, simply reads the body, JSON-deserializes it into the expected event type, and converts it into a `SignerEvent` that is immediately forwarded to the signer's decision-making runloop: [2](#0-1) 

Compare this to the node-side HTTP RPC endpoints in `stackslib/src/net/api/`, all of which gate sensitive POST endpoints (block proposal, block replay, block simulation, fast-call-read-only, transaction simulation) behind an `authorization` header equality check against a configured password: [3](#0-2) [4](#0-3) 

No equivalent auth gate exists on the signer's receiving side. A search of the entire `libsigner` crate for `Authorization`/`auth_token` returns no results, confirming the absence of any credential check on this listener. This breaks the intended equality "message came from the paired Stacks node" vs. "message came from anywhere on the network reachable to the bound port" — the signer treats any TCP client's POST as authentic.

Note that `StackerDBChunksEvent` chunks that map to `SIGNERS_NAME`/`MINERS_NAME` boot contracts are further processed by `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` which does recover a public key per chunk via `chunk.recover_pk()`: [5](#0-4) 
—but this only extracts *which* key signed the (attacker-supplied) chunk; it does not authenticate the transport itself, and the `BlockValidationResponse`, `NewBurnBlock`, and `NewBlock` event types carry **no signature field at all**, so they are accepted purely on the strength of "some TCP peer POSTed valid JSON to this path."

### Impact Explanation
Any host that can reach the TCP port that `SignerEventReceiver::bind` listens on (the signer's configured `endpoint`) can inject forged `BlockValidateResponse`, `NewBurnBlock`, `NewBlock`, or `StackerDBChunksEvent` payloads directly into the signer's event stream without any credential. This is an unauthenticated write into a component's trusted event pipeline — matching the "unauthenticated/unauthorized write to state" and "forged-data propagation" impact classes, since these events are what the signer's runloop consumes to decide how to respond to block proposals (although the decision logic itself is out of scope for this analysis, the point of failure — a completely open, unauthenticated ingestion point — sits squarely in `libsigner`'s transport layer, which is in scope).

### Likelihood Explanation
Exploitation only requires network reachability to the signer's bound HTTP port and knowledge of the JSON schemas for `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` (all public, documented types re-exported from `libsigner`). No secrets, node keys, or privileged roles are required — this is a remote, unprivileged issue as long as the port is exposed (e.g., not strictly firewalled to localhost/the paired node's IP, which is an operational assumption, not an enforced control in code).

### Recommendation
Add a mandatory shared-secret/HMAC or mTLS check to `SignerEventReceiver::next_event`/`process_event`, analogous to the `authorization` header checks already used on node-side RPC endpoints (e.g., `postblock_proposal.rs`, `fastcallreadonly.rs`), and reject any request lacking a valid credential before deserializing or forwarding the event.

### Proof of Concept
```
# Assuming a stacks-signer listening on 127.0.0.1:30000 (or a non-loopback bind address)
BODY='{"contract_id":{"issuer":[26,[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]],"name":"signer-0-3"},"modified_slots":[]}'
printf 'POST /stackerdb_chunks HTTP/1.1\r\nHost: 127.0.0.1:30000\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s' \
  "${#BODY}" "$BODY" | nc 127.0.0.1 30000
# No credential of any kind is required; the server (libsigner/src/events.rs) accepts and forwards this to the signer runloop.
``` [6](#0-5)

### Citations

**File:** libsigner/src/events.rs (L410-459)
```rust
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

**File:** libsigner/src/events.rs (L596-603)
```rust
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1136-1144)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L101-110)
```rust
        // If no authorization is set, then the block proposal endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```
