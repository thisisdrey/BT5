### Title
Unauthenticated event-injection into the stacks-signer's HTTP event receiver - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` accepts and processes HTTP POST requests on the signer's bound listener socket (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/shutdown`) without checking any authentication token, source address, or credential on the inbound request. [1](#0-0)  This mirrors the reported bug class (an auth gate that is expected/assumed but does not actually gate access), except here there is no gate at all rather than a bypassable one.

### Finding Description
The signer process binds an HTTP server (`http_server.recv()`) that the Stacks node is expected to `POST` events to, and `next_event` dispatches purely based on URL path with zero authentication check: [2](#0-1) 

Compare this to the node-side endpoints that *do* gate sensitive writes: `/v3/block_proposal` explicitly requires and compares an `Authorization` header against a configured password before accepting a block proposal. [3](#0-2)  No equivalent check exists anywhere in the `EventReceiver`/`SignerEventReceiver` implementation for the signer's own inbound listener, even though this listener directly feeds trusted-looking data (`BlockValidateResponse` on `/proposal_response`, `BurnBlockEvent` on `/new_burn_block`, `StackerDBChunksEvent` on `/stackerdb_chunks`) into the signer runloop via `forward_event`/channel without any re-verification step at the transport layer. [4](#0-3) 

Some of these event types are partially self-authenticating downstream (e.g., signer-to-signer `StackerDBChunksEvent` messages are validated via `chunk.recover_pk()`/signature checks before being trusted, as seen in `try_from` for `SignerEvent`). [5](#0-4)  However, `BlockValidateResponse` (posted to `/proposal_response`) and `BurnBlockEvent` (posted to `/new_burn_block`) carry no such signature check at the transport or event-parsing layer — they are trusted purely because the HTTP request reached the bound socket. Any host that can reach the signer's bind address (which is a user-configured `SocketAddr`, not hard-restricted to loopback) can therefore inject forged `BlockValidateResponse` and `BurnBlockEvent` payloads directly into the signer's processing pipeline.

### Impact Explanation
An unauthenticated network peer able to reach the signer's event-receiver port can inject forged validation results or burn-block events into the signer without any credential. Depending on deployment (signer bound to a non-loopback interface, exposed via port-forwarding/reverse proxy, or reachable inside a shared network), this is a path for unauthorized write of forged data into signer-local state — matching the "forged gossip relayed" / "unauthenticated ... write to state" class of impact called out in scope. This is a transport-layer authentication gap in `libsigner`, independent of the signer's internal decision logic (which is explicitly out of scope and not what this finding relies on).

### Likelihood Explanation
Exploitability depends entirely on network exposure: if the signer's event-receiver bind address is restricted to `127.0.0.1` by all deployment configurations, remote exploitation is not possible. I was unable to fully confirm, within the available context, whether the documentation (`docs/signing.md`) or default sample configs enforce loopback-only binding for the signer's event-receiver endpoint, or whether operators are expected/able to expose it more broadly (e.g., in containerized or multi-host signer deployments). This is a real gap in my verification and should be checked directly against `docs/signing.md` and the signer's `endpoint` config handling before treating this as remotely exploitable in a default/typical deployment.

### Recommendation
Add an authentication check (e.g., a shared secret/HMAC or mTLS) to `SignerEventReceiver::next_event` before dispatching to `process_event`, mirroring the `Authorization` header check already used for `/v3/block_proposal` on the node side. At minimum, require and verify a token configured out-of-band between the node and the signer for every POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block`, and reject unauthenticated requests with 401 rather than processing them.

### Proof of Concept
1. Start a `stacks-signer` instance with its event-receiver bound to an address reachable from the attacker's network position (per its `endpoint` config).
2. From an unauthenticated host, send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:<port>
Content-Type: application/json
Content-Length: <n>

{<forged BlockValidateResponse JSON>}
```
3. `SignerEventReceiver::next_event` accepts the request without any credential check, deserializes it via `process_event`, and forwards the resulting `SignerEvent` to the signer runloop through `forward_event`. [6](#0-5) 
4. No 401/403 is ever returned; the forged event is processed exactly like a legitimate one from the local node.

### Citations

**File:** libsigner/src/events.rs (L404-459)
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
    }
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

**File:** stackslib/src/net/api/postblock_proposal.rs (L1135-1144)
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
