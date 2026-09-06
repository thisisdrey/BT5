### Title
Unauthenticated remote shutdown / forged-event injection into `stacks-signer` via `SignerEventReceiver` - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event()` — the HTTP handler that accepts event pushes from a Stacks node on the signer's listening port — never validates any authentication token/header on incoming requests, despite the node/signer configuration model (`auth_token` / `auth_password`) explicitly being documented as a required, matching secret between the two components. Any remote party that can reach the signer's bind address can therefore inject forged `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent` payloads, or send a bare `POST /shutdown` to unconditionally terminate the signer's event loop.

### Finding Description
`SignerEventReceiver::next_event()` dispatches purely on HTTP method + URL path with no authentication check at all: [1](#0-0) 

Compare this to the sibling node-side RPC surface (`stackslib/src/net/api/postblock_proposal.rs`, `stackslib/src/net/httpcore.rs`), which explicitly gates a privileged endpoint behind an `auth_token` header comparison, e.g. the block-proposal handler pattern seen in `RPCNakamotoBlockReplayRequestHandler::try_parse_request`: [2](#0-1) 

The signer side has an equivalent expectation — the shipped docs and sample configs instruct operators to set a shared secret (`auth_token` on the node, `auth_password` on the signer) that "must match": [3](#0-2) [4](#0-3) 

However, nowhere in `libsigner` (`events.rs`, `http.rs`) is any header compared against this configured value before an event is accepted and forwarded into the signer's runloop channel. `decode_http_request` in `libsigner/src/http.rs` parses headers into a map but that map is never consulted for an `authorization` field by `SignerEventReceiver`: [5](#0-4) 

This breaks the intended equality "request came from the authenticated node" vs. "request came from any TCP peer that can reach the bind address" — the auth gate fails open because it was never wired up on the receiving side. The `/shutdown` path is the most severe consequence: it flips `stop_signal` and returns `Terminated`, unconditionally exiting the event receiver's `main_loop()`: [6](#0-5) [7](#0-6) 

Once the event-receiver thread exits, the `Sender` side of the inter-thread channel is dropped, and `SignerRunLoop::main_loop()` observes `RecvTimeoutError::Disconnected` and exits as well, ending the whole signer process's runloop: [8](#0-7) 

The other endpoints (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) similarly accept and forward attacker-controlled JSON bodies into `SignerEvent` without any authentication, only bounded by JSON/type deserialization success: [9](#0-8) 

Note that for the `MINERS_NAME` StackerDB contract lane, chunk messages are deserialized and forwarded as `SignerEvent::MinerMessages` without any signature/`recover_pk` check at this layer, relying entirely on the (bypassable) assumption that only the authenticated node feeds this endpoint: [10](#0-9) 

### Impact Explanation
This is a remote, unauthenticated denial-of-service against `stacks-signer` (a single crafted `POST /shutdown` HTTP request halts the signer's event processing and runloop, aligning with "remote crash/unauthenticated DoS from few messages"), and additionally an unauthenticated-write vector: an attacker can inject spoofed `SignerEvent`s (fake burn blocks, fake block-validation responses, fake miner/StackerDB messages) into the signer's decision pipeline, which is a state-integrity concern for a component that participates in block signing. Given the shipped sample configuration explicitly recommends binding the signer's event endpoint on `0.0.0.0` (network-reachable) and states an `auth_token`/`auth_password` "must match" as the protection mechanism, the missing enforcement is a direct fail-open of the intended access control.

### Likelihood Explanation
High. No credentials, signatures, or special network position are required — only network reachability to the configured `endpoint` (which the documentation and sample configs show being bound non-locally). The `/shutdown` request requires no body and no headers beyond a minimal HTTP request line.

### Recommendation
Enforce the configured `auth_password`/`auth_token` shared secret as a required `Authorization` (or custom) header check inside `SignerEventReceiver::next_event()`/`decode_http_request` before dispatching to any handler, rejecting with 401 on mismatch or absence, mirroring the node-side pattern already used in `stackslib/src/net/api/postblock_proposal.rs`. At minimum, gate the `/shutdown` path and all event-forwarding paths behind this check.

### Proof of Concept
1. Start a `stacks-signer` configured per `docs/signing.md`, with its event-receiver `endpoint` reachable over the network (e.g. `0.0.0.0:30000`, as shown in sample configs).
2. From any remote host with network access to that port, send:
   ```
   POST /shutdown HTTP/1.1
   Host: <signer-endpoint>
   Connection: close
   Content-Length: 0

   ```
3. Observe (per `libsigner/src/events.rs:443-445` and `runloop.rs:66-82`) that the event receiver sets its stop signal and terminates, and the signer's runloop subsequently exits due to channel disconnection — no authentication was required.
4. Alternatively, POST a crafted JSON body to `/stackerdb_chunks`, `/new_burn_block`, `/new_block`, or `/proposal_response` to inject a forged `SignerEvent` into the runloop, as done by the test harness in `libsigner/src/tests/mod.rs:118-146`, but from an unauthenticated remote peer instead of the trusted node.

### Citations

**File:** libsigner/src/events.rs (L284-312)
```rust
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

**File:** docs/signing.md (L53-59)
```markdown
These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```

**File:** sample/conf/testnet-miner-conf.toml (L73-87)
```text
# ============================================================
# [connection_options] - Authentication for signer communication
# ============================================================
[connection_options]
# WARNING: Must match the signer's auth_password.
auth_token = "<YOUR_AUTH_TOKEN>"

# ============================================================
# [[events_observer]] - Signer event subscription
# ============================================================

# WARNING: endpoint must match your signer's endpoint config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```

**File:** libsigner/src/http.rs (L30-58)
```rust
/// Decoding of the relevant parts of a signer-directed HTTP request from the Stacks node
#[derive(Debug)]
pub struct SignerHttpRequest {
    pub verb: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    pub body_offset: usize,
}

impl SignerHttpRequest {
    pub fn new(
        verb: String,
        path: String,
        headers: HashMap<String, String>,
        body_offset: usize,
    ) -> SignerHttpRequest {
        SignerHttpRequest {
            verb,
            path,
            headers,
            body_offset,
        }
    }

    /// Decompose into (verb, path, headers, body-offset)
    pub fn destruct(self) -> (String, String, HashMap<String, String>, usize) {
        (self.verb, self.path, self.headers, self.body_offset)
    }
}
```

**File:** libsigner/src/runloop.rs (L66-82)
```rust
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
