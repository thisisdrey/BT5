### Title
Unauthenticated `/new_block` event endpoint allows forging arbitrary `SignerEvent::NewBlock` transaction sets - (File: libsigner/src/events.rs)

### Summary
The `SignerEventReceiver`'s HTTP server accepts unauthenticated `POST /new_block` requests and deserializes the body directly into a `StacksBlockEvent`, which is then converted via `TryFrom<StacksBlockEvent> for SignerEvent<T>` into `SignerEvent::NewBlock` with no cryptographic validation of the block hash, consensus hash, or the embedded transactions. Anyone who can reach the signer's configured event-listener TCP port can inject a fabricated block/transaction set that is forwarded verbatim to the signer runloop.

### Finding Description
`SignerEventReceiver::next_event` dispatches on `request.url()`; for `/new_block` it calls `process_event::<T, StacksBlockEvent>(request)` [1](#0-0) . Nowhere in `next_event`, `with_server`, or `process_event` is there a check of a shared secret, bearer token, signature, or peer address before processing the body [2](#0-1) . `process_event` only reads the body, JSON-deserializes it into `StacksBlockEvent`, and calls `try_into()` [3](#0-2) .

`StacksBlockEvent::transactions` is populated by `deserialize_raw_tx_hex`, which takes attacker-supplied hex strings and calls `StacksTransaction::consensus_deserialize` on them with no signature or origin check [4](#0-3) . `TryFrom<StacksBlockEvent> for SignerEvent<T>` then copies `index_block_hash`, `consensus_hash`, `block_height`, and the decoded `transactions` straight into `SignerEvent::NewBlock` with no cross-check against any chain state, no verification the reported `index_block_hash`/`consensus_hash` actually correspond to a block containing those transactions [5](#0-4) . The equality that should hold — "block/tx set forwarded to runloop == block/tx set actually processed by the paired node" — is broken: the only source of truth is whatever bytes arrive on the socket.

The attacker's message: a raw HTTP `POST /new_block` with a JSON body containing arbitrary `index_block_hash`, `consensus_hash`, `block_hash`, `block_height`, and a `transactions` array of `{ "raw_tx": "0x..." }` entries encoding a self-signed, well-formed `StacksTransaction` (need not be validly signed against any real account, since only `consensus_deserialize` is invoked, not verification). This is trivially repeatable for every message sent.

### Impact Explanation
Any party able to open a TCP connection to the signer's event-listener port can push forged `SignerEvent::NewBlock` events, controlling `block_id`, `consensus_hash`, `block_height`, and the full `transactions: Vec<StacksTransaction>` fields observed downstream by the signer runloop, with zero authentication check in this code path. Because there is no verification tying the reported hash/consensus data to the actual transactions, or to real on-chain state, a remote party can repeatedly inject fabricated chain data into the signer process at will.

### Likelihood Explanation
No privileged role, secret, or signature is required — only network reachability to the TCP port on which `SignerEventReceiver::bind` listens (configured via the signer's `endpoint` setting). If that port is reachable from outside the trusted node-to-signer network segment (a common real-world case where signer and node run on separate hosts), any remote unprivileged party can exploit this at will, repeatedly, at negligible cost (a single crafted HTTP POST per event).

### Recommendation
Add authentication to the signer's event-receiver HTTP server (e.g., a pre-shared bearer token or mTLS matching what the paired node's event dispatcher is configured with), and/or bind the listener to a loopback/private interface by default with explicit opt-in for exposing it more broadly. Reject requests whose source doesn't match the configured trusted event-dispatcher address as a minimum mitigation, and document that this endpoint must never be exposed to untrusted networks.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
// 1. Bind a SignerEventReceiver<SomeSignerMessageType> to 127.0.0.1:<port> via `bind()`.
// 2. Spawn a thread calling `next_event()`.
// 3. From a separate "attacker" TcpStream (no credentials), send:
//    POST /new_block HTTP/1.1
//    Host: 127.0.0.1:<port>
//    Content-Type: application/json
//    Content-Length: <n>
//
//    {"index_block_hash":"0x<attacker_hash>","consensus_hash":"0x<attacker_hash20>",
//     "block_hash":"0x<attacker_hash20>","block_height":999999999,
//     "transactions":[{"raw_tx":"0x<hex of a StacksTransaction the attacker built>"}]}
// 4. Assert next_event() returns Ok(SignerEvent::NewBlock { transactions, .. })
//    where transactions[0] == the attacker-fabricated StacksTransaction,
//    proving the forged payload was accepted without any authentication check.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** libsigner/src/events.rs (L404-458)
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

**File:** libsigner/src/events.rs (L661-688)
```rust
impl NewBlockTransaction {
    pub fn get_stacks_transaction(&self) -> Result<Option<StacksTransaction>, CodecError> {
        if self.raw_tx == "00" {
            Ok(None)
        } else {
            let tx_bytes = hex_bytes(&self.raw_tx).map_err(|e| {
                CodecError::DeserializeError(format!("Failed to deserialize raw tx: {e}"))
            })?;
            let tx = StacksTransaction::consensus_deserialize(&mut &tx_bytes[..])?;
            Ok(Some(tx))
        }
    }
}

/// "Special" deserializer to turn `{ tx_raw: "0x..." }` into `StacksTransaction`.
fn deserialize_raw_tx_hex<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> Result<Vec<StacksTransaction>, D::Error> {
    let tx_objs: Vec<NewBlockTransaction> = serde::Deserialize::deserialize(d)?;
    Ok(tx_objs
        .iter()
        .map(|tx| tx.get_stacks_transaction())
        .collect::<Result<Vec<_>, _>>()
        .map_err(serde::de::Error::custom)?
        .into_iter()
        .flatten()
        .collect::<Vec<_>>())
}
```

**File:** libsigner/src/events.rs (L690-720)
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
