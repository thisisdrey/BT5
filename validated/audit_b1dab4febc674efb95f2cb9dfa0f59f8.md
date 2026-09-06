### Title
Unauthenticated `/shutdown` and event endpoints on the signer's HTTP event listener allow remote DoS and forged event injection - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` (the HTTP server the `stacks-signer` binds to receive events pushed by a Stacks node) applies **no authentication whatsoever** to incoming requests. Every path — `/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/shutdown` — is dispatched purely based on URL and HTTP method, with no check of source, token, or credential. This mirrors the Hoverfly `/api/v2/ws/logs` bug class: an endpoint that should be gated behind the same trust boundary as the rest of the system is instead wide open to any remote/unprivileged caller who can reach the listening socket.

### Finding Description
`SignerEventReceiver::bind` opens a plain `tiny_http::HttpServer` on the configured `endpoint`: [1](#0-0) 

`next_event` then routes any inbound request purely by URL/method with no authentication check at all: [2](#0-1) 

Notably:
- `/shutdown` immediately sets `stop_signal` to `true` and returns `EventError::Terminated`, with zero credential check: [3](#0-2) 
- `main_loop` breaks out of the receive loop on `EventError::Terminated`, permanently ending the signer's ability to receive further node events until restarted: [4](#0-3) 
- `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` all deserialize an attacker-supplied JSON body directly into event types and forward them into the signer's runloop, again without any origin or credential check: [5](#0-4) 
- Critically, for `StackerDBChunksEvent`s addressed to the `.miners` boot contract, the payload is deserialized into miner messages with **no signature verification at all** — the code just calls `T::consensus_deserialize` on the attacker-controlled `chunk.data`: [6](#0-5) 

The HTTP framing itself (`libsigner/src/http.rs::decode_http_request`) parses method/path/headers with no auth/token concept present in the header table it builds: [7](#0-6) 

By contrast, every comparable admin-style endpoint on the *node* side (`stackslib/src/net/api/*`) explicitly checks a shared secret token before accepting the request, e.g. block-proposal/replay/simulate endpoints all gate on `self.auth`: [8](#0-7) [9](#0-8) 

This confirms the intended security model in this codebase is "sensitive control/data endpoints require a shared auth token," and the signer's own event listener — which is meant to only accept pushes from the paired, trusted node — is the one place that fails to enforce it, exactly analogous to Hoverfly's WS logs endpoint bypassing the REST auth middleware.

### Impact Explanation
Any host that can reach the configured signer `endpoint` (which per `docs/signing.md`/sample configs is a plain `host:port`, and is commonly reachable beyond localhost in multi-host signer/miner deployments) can:
1. Send a single unauthenticated `POST /shutdown` and immediately kill the signer's event-receiver thread, cutting the signer off from all future node-pushed events (StackerDB chunk pushes, burn-block notifications, proposal-response callbacks) — an unauthenticated remote DoS from a single request, matching the "Critical: remote crash/unauthenticated DoS from few messages" bucket.
2. Inject a forged `StackerDBChunksEvent` for the `.miners` boot contract that is deserialized with zero signature verification, directly feeding attacker-controlled `SignerEvent::MinerMessages` into the signer's runloop — an unauthenticated forged-data injection into the signer process.

Both are reachable by an unprivileged remote party with no node secret, no signer key, and no admin role, satisfying the "auth-gate that fails open" class explicitly called out as in-scope.

### Likelihood Explanation
High. No credentials, no cryptographic material, and no special network position are required beyond reaching the listening TCP port; a single crafted HTTP request suffices for the `/shutdown` DoS, and the routing/dispatch logic is unconditionally reachable for every listed path.

### Recommendation
Add an authentication check (e.g., a shared-secret header comparable to the node's `auth_token`) enforced in `SignerEventReceiver::next_event` (or in `decode_http_request`/`SignerHttpRequest`) before dispatching to any handler, especially `/shutdown` and the event-ingest paths. Additionally, always verify the recovered/expected signer key against the contract's known signer set before trusting `StackerDBChunksEvent` contents, including for the `.miners` lane, rather than relying solely on deserialization success.

### Proof of Concept
1. Start a `stacks-signer` bound to `endpoint = "0.0.0.0:30000"` (or any host/port reachable by the attacker) per `sample/conf/signer/mainnet-signer-conf.toml`.
2. From an unauthenticated remote host, send:
```
POST /shutdown HTTP/1.1
Host: <signer-host>:30000
Connection: close
Content-Length: 5
Content-Type: text/plain

pwned
```
3. The signer's `SignerEventReceiver::next_event` matches `request.url() == "/shutdown"`, sets `stop_signal = true`, and returns `EventError::Terminated`; `main_loop` breaks, permanently stopping the signer's event-receiver thread with no re-authentication or restart — reproducing the same test harness pattern already present in `libsigner/src/tests/mod.rs` (`SignerStopSignaler::send`), but from an arbitrary, unauthenticated network peer instead of the node itself. [10](#0-9)

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
```

**File:** libsigner/src/events.rs (L376-396)
```rust
impl EventStopSignaler for SignerStopSignaler {
    #[cfg_attr(test, mutants::skip)]
    fn send(&mut self) {
        self.stop_signal.store(true, Ordering::SeqCst);
        // wake up the thread so the atomicbool can be checked
        // This makes me sad...but for now...it works.
        if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
            // We need to send actual data to trigger the event receiver
            let body = "Yo. Shut this shit down!".to_string();
            let req = format!(
                "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
                self.local_addr,
                body.len(),
                body
            );
            if let Err(e) = stream.write_all(req.as_bytes()) {
                error!("Failed to send shutdown request: {}", e);
            }
        }
    }
}
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

**File:** libsigner/src/events.rs (L517-542)
```rust
// TODO: add tests from mutation testing results #4835
#[cfg_attr(test, mutants::skip)]
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

**File:** libsigner/src/http.rs (L61-123)
```rust
/// Returns (verb, path, table of headers, body_offset) on success
pub fn decode_http_request(payload: &[u8]) -> Result<SignerHttpRequest, EventError> {
    // realistically, there won't be more than 32 headers
    let mut headers_buf = [httparse::EMPTY_HEADER; MAX_HTTP_HEADERS];
    let mut req = httparse::Request::new(&mut headers_buf);
    let (verb, path, headers, body_offset) =
        if let Ok(httparse::Status::Complete(body_offset)) = req.parse(payload) {
            // version must be valid
            match req
                .version
                .ok_or(EventError::MalformedRequest("No HTTP version".to_string()))?
            {
                0 => {}
                1 => {}
                _ => {
                    return Err(EventError::MalformedRequest(
                        "Invalid HTTP version".to_string(),
                    ));
                }
            };

            let verb = req
                .method
                .ok_or(EventError::MalformedRequest("No HTTP method".to_string()))?
                .to_string();
            let path = req
                .path
                .ok_or(EventError::MalformedRequest("No HTTP path".to_string()))?
                .to_string();

            let mut headers: HashMap<String, String> = HashMap::new();
            for i in 0..req.headers.len() {
                let value = String::from_utf8(req.headers[i].value.to_vec()).map_err(|_e| {
                    EventError::MalformedRequest("Invalid HTTP header value: not utf-8".to_string())
                })?;
                if !value.is_ascii() {
                    return Err(EventError::MalformedRequest(
                        "Invalid HTTP request: header value is not ASCII-US".to_string(),
                    ));
                }
                if value.len() > MAX_HTTP_HEADER_LEN {
                    return Err(EventError::MalformedRequest(
                        "Invalid HTTP request: header value is too big".to_string(),
                    ));
                }

                let key = req.headers[i].name.to_string().to_lowercase();
                if headers.get(&key).is_some() {
                    return Err(EventError::MalformedRequest(format!(
                        "Invalid HTTP request: duplicate header \"{key}\""
                    )));
                }
                headers.insert(key, value);
            }
            (verb, path, headers, body_offset)
        } else {
            return Err(EventError::Deserialize(
                "Failed to decode HTTP headers".to_string(),
            ));
        };

    Ok(SignerHttpRequest::new(verb, path, headers, body_offset))
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

**File:** stackslib/src/net/api/blockreplay.rs (L574-583)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
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
