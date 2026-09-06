### Title
Missing Authentication on the Signer's Event-Receiver HTTP Endpoint Allows Unauthenticated Injection of Block-Validation, Burn-Block, and StackerDB Events - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver`, which listens on the `[[events_observer]] endpoint` address configured for `stacks-signer` (e.g. `endpoint = "0.0.0.0:30000"` in `sample/conf/signer/mainnet-signer-conf.toml`), accepts and processes any HTTP `POST` request without any authentication check. This is the exact analog of the reported bug class (component API endpoint reachable without authentication): the stacks-node's own `/v3/block_proposal` and `/v3/blocks/upload/?broadcast=1` endpoints are authenticated by the shared `auth_token`/`Authorization` header, but the signer's own inbound event endpoint has no equivalent check.

### Finding Description
`SignerEventReceiver::next_event` in [1](#0-0)  dispatches any incoming request purely by URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown`) and hands the body straight to `process_event`, which deserializes it into a `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` and forwards it into the signer's event channel via `forward_event` at [2](#0-1) . There is no verification of a shared secret, HMAC, or any other credential on the incoming request anywhere in `libsigner/src/events.rs` or `libsigner/src/runloop.rs` — a `grep` for `authorization`/`auth_token` across `libsigner/**` returns zero matches.

By contrast, the stacks-node side deliberately gates its equivalent trust boundary: `RPCBlockProposalRequestHandler::try_parse_request` in [3](#0-2)  and the broadcast path in `RPCPostBlockRequestHandler::try_parse_request` in [4](#0-3)  both require the `authorization` header to equal the configured `auth_token` before accepting data. The `auth_token` config option is explicitly documented as securing "the communication channel between this node and a connected `stacks-signer` instance" in [5](#0-4) , yet that protection is only enforced on the node's inbound endpoints — not on the signer's inbound event-receiver endpoint that the node is supposed to push events to. This breaks the intended equality "only the paired node may deliver events to this signer" — the endpoint accepts data from any source that can reach the socket.

### Impact Explanation
The `endpoint` the signer binds is operator-configurable and shown bound to `0.0.0.0` in the shipped reference config [6](#0-5) . Any network peer that can reach that port can POST a forged `BlockValidateResponse`, `StackerDBChunksEvent`, or `BurnBlockEvent`/`StacksBlockEvent`, which is fed unauthenticated directly into the signer's `SignerRunLoop::main_loop` via the channel populated by `forward_event` ( [7](#0-6) ; consumed in [8](#0-7) ). This is an unauthenticated write into the signer's internal event stream — the signer has no way to distinguish a genuine event pushed by its paired node from one forged by an attacker, undermining the "authenticated node → signer" trust assumption the `auth_token` mechanism was designed to establish.

### Likelihood Explanation
Exploitation requires only network reachability to the signer's configured event-listener port and knowledge of the (undocumented-to-attacker but discoverable) URL paths, which are fixed strings in the public source code. No credentials, node secret, or signer key are needed to reach and post to the endpoint itself.

### Recommendation
Require signer-side authentication (e.g., a shared secret / HMAC over the same `auth_token`, or mTLS) on `SignerEventReceiver`'s HTTP endpoint in `libsigner/src/events.rs`, verified in `next_event` before deserializing/forwarding any event, mirroring the `authorization` header checks already present in `stackslib/src/net/api/postblock_proposal.rs` and `postblock_v3.rs`. Additionally, sample configs should default the signer's `endpoint` to a loopback/private-only bind and documentation should warn against exposing it publicly.

### Proof of Concept
1. Deploy a `stacks-signer` using the reference config, which binds its event endpoint to `0.0.0.0:30000` per [6](#0-5) .
2. From any host that can reach `<signer-ip>:30000`, send: `POST /proposal_response HTTP/1.1` with a JSON body deserializable as `BlockValidateResponse`.
3. `SignerEventReceiver::next_event` ( [9](#0-8) ) matches the path, calls `process_event::<T, BlockValidateResponse>`, and — without checking any credential — forwards the attacker-controlled event into the signer's run loop as if it had come from the paired stacks-node.

### Citations

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

**File:** stackslib/src/net/api/postblock_proposal.rs (L1128-1144)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
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

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** libsigner/src/runloop.rs (L59-82)
```rust
    fn main_loop<EVST: EventStopSignaler>(
        &mut self,
        event_recv: Receiver<SignerEvent<T>>,
        result_send: Sender<R>,
        mut event_stop_signaler: EVST,
    ) -> Option<R> {
        info!("Signer runloop begin");
        loop {
            let poll_timeout = self.get_event_timeout();
            let next_event_opt = match event_recv.recv_timeout(poll_timeout) {
                Ok(event) => Some(event),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => {
                    info!("Event receiver disconnected");
                    return None;
                }
            };
            if let Some(final_state) = self.run_one_pass(next_event_opt, &result_send) {
                info!("Runloop exit; signaling event-receiver to stop");
                event_stop_signaler.send();
                return Some(final_state);
            }
        }
    }
```
