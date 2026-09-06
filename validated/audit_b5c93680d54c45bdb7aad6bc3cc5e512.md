### Title
Unauthenticated `SignerEvent` injection via `SignerEventReceiver::next_event`/`process_event` - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` and the generic `process_event::<T, StackerDBChunksEvent>` accept any HTTP POST to `/stackerdb_chunks` on the signer's bound socket and deserialize the body directly into a `StackerDBChunksEvent`, with no check that the caller is the local `stacks-node`. For the `.miners` boot contract branch of `TryFrom<StackerDBChunksEvent>`, the resulting `SignerEvent::MinerMessages` is produced with zero per-chunk signature verification, so an attacker who can reach the bound socket can inject arbitrary miner messages into the signer's runloop channel.

### Finding Description
`next_event` routes any POST to `/stackerdb_chunks` straight into `process_event::<T, StackerDBChunksEvent>` [1](#0-0) . `process_event` reads the raw body, JSON-deserializes it into `StackerDBChunksEvent`, and calls `.try_into()` with no authentication, no header check, no peer-address check [2](#0-1) . `bind()` simply opens a `tiny_http::Server` on the configured socket with no auth layer at all [3](#0-2) .

In `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`, when `contract_id` matches the `.miners` boot contract, each `modified_slots` chunk is deserialized directly via `T::consensus_deserialize` with **no signature recovery/verification at all**, unlike the `signers-X-Y` branch which calls `chunk.recover_pk()` [4](#0-3) , contrasted with [5](#0-4) . This confirms the claimed fault: the library surfaces `SignerEvent::MinerMessages` to the runloop without verifying the message came from an authorized StackerDB slot owner (or from the local node at all).

The `auth_password`/`auth_token` mechanism documented in `docs/signing.md` and wired through `stacks-signer/src/config.rs` / `stacks-signer/src/client/stacks_client.rs` authenticates the **signer-to-node RPC client** calls, not the **node-to-signer event push** that `SignerEventReceiver` listens for; nothing in `events.rs` reads or checks any such token on inbound requests [6](#0-5) .

### Impact Explanation
An attacker who can open a TCP connection to the signer's bound event-receiver address can forge a `StackerDBChunksEvent` JSON body targeting the `.miners` contract and have it deserialized into `SignerEvent::MinerMessages` with attacker-controlled contents, with no signature check performed in this code path. This is an unauthenticated write into the signer's internal event stream, repeatable per-connection/per-message, matching the "Critical: unauthenticated write to internal state" category — provided the event-receiver socket is reachable beyond loopback.

### Likelihood Explanation
Exploitability is entirely conditioned on the signer's `bind()` address (the `endpoint` in the signer's config, matched to the node's `[[events_observer]] endpoint`) being reachable from the attacker's network. Sample/documented configurations bind this to `127.0.0.1:30000` (loopback-only) [7](#0-6) , which is the intended, documented deployment topology for this node↔signer channel — analogous to the stacks-node's own event-observer interface, which is likewise unauthenticated by design and expected to run on a trusted/private network segment [8](#0-7) . There is no code-level authentication gap being exploited beyond the pre-existing, documented trust boundary; the vulnerability only manifests if an operator mis-binds the receiver to a non-loopback/public address, which is an atypical/non-default configuration, not a flaw introduced or fixable purely within `events.rs`'s logic against its documented threat model.

### Recommendation
Add authentication to the `SignerEventReceiver` HTTP endpoint (e.g., a shared-secret header checked in `process_event`/`next_event` before deserializing, mirroring the `auth_token`/`auth_password` mechanism already used for the reverse direction), and/or restrict `SignerEventReceiver::bind` to loopback by default with an explicit opt-in warning for non-loopback binding. Additionally, apply the same `recover_pk`/signature-verification discipline used for the `signers-X-Y` branch to the `.miners` branch in `TryFrom<StackerDBChunksEvent>` so that `SignerEvent::MinerMessages` chunks are also authenticated before being forwarded.

### Proof of Concept
Rust test in `libsigner`:
1. Create a `SignerEventReceiver::<SignerMessage>::new(false)`, call `bind("127.0.0.1:0")`, `add_consumer(tx)`, spawn `next_event()` in a thread.
2. Open a raw `TcpStream` to the bound address and send:
```
POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: <n>\r\nContent-Type: application/json\r\n\r\n{"contract_id":{"issuer":[...boot addr...],"name":"miners"},"modified_slots":[{"slot_id":0,"slot_version":1,"sig":[...64 zero/garbage bytes...],"data":[...attacker bytes that decode via T::consensus_deserialize...]}]}
```
3. Assert `next_event()` returns `Ok(SignerEvent::MinerMessages(msgs))` and that `msgs` contains the attacker-fabricated content, with no error raised for the missing/garbage signature — demonstrating the `.miners` branch of `TryFrom<StackerDBChunksEvent>` (`libsigner/src/events.rs:549-567`) performs no signature check before forwarding to the consumer channel via `forward_event`.

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
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

**File:** sample/conf/mainnet-miner-conf.toml (L429-436)
```text
# Signer event observer (REQUIRED for signer integration).
#
# WARNING: The `endpoint` must match your signer's `endpoint` config.
# The `events_keys` must include "stackerdb", "block_proposal", and
# "burn_blocks" for proper signer operation.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```

**File:** docs/event-dispatcher.md (L1-24)
```markdown
# Event Dispatching / Observer Interface

The `stacks-node` supports a configurable event observer interface, allowing external services to subscribe to various on-chain and node-related events. This is enabled by adding one or more `[[events_observer]]` entries to the node's `config.toml` file.

```toml
...
[[events_observer]]
endpoint = "listener:3700" # The host and port of your listening service
events_keys = ["*"]                     # A list of event keys to subscribe to (see below)
timeout_ms = 5000                       # Optional: Timeout in milliseconds for requests (default: 1000)
disable_retries = false                 # Optional: If true, failed deliveries won't be retried (default: false)
disable_contract_interface = false      # Optional: If true, the contract_interface (ABI) field of transactions in new_block / new_microblocks payloads sent to this observer is null (default: false)

# Example of another observer for specific events
# [[events_observer]]
# endpoint = "another-service:3701"
# events_keys = [
#   "stx",
#   "ST0000000000000000000000000000000000000000.my-contract::my-event"
# ]
...
```

The `stacks-node` will then execute HTTP POST requests with JSON payloads to the configured `endpoint` for the subscribed events.
```
