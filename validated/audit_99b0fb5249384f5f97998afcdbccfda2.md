This confirms the finding: the `/new_block` HTTP endpoint on `SignerEventReceiver` performs **no authentication whatsoever** on the sender. The code explicitly warns about this design gap and relies entirely on network-level isolation.

### Title
Unauthenticated `/new_block` HTTP endpoint allows forged `SignerEvent::NewBlock` injection with attacker-controlled transactions - (File: `libsigner/src/events.rs`)

### Summary
The signer's event-receiver HTTP server accepts any POST to `/new_block` and deserializes it directly into a `SignerEvent::NewBlock`, including a `transactions` field populated via `deserialize_raw_tx_hex` from attacker-supplied hex strings, with no verification that the request originated from the locally configured Stacks node. Any party able to reach the bound socket can forge block-content events that the signer processes as if they were reported by its own node.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` routes any POST to `/new_block` straight to `process_event::<T, StacksBlockEvent>(request)` [1](#0-0) . `process_event` reads the raw body, JSON-decodes it into `StacksBlockEvent`, and converts it via `TryFrom` into `SignerEvent::NewBlock` with no signature, secret, or peer-identity check performed anywhere in this path [2](#0-1) .

`StacksBlockEvent` deserializes `index_block_hash`, `consensus_hash`, `block_hash`, `block_height`, and `transactions` (via `deserialize_raw_tx_hex`, which decodes each `raw_tx` hex string with `StacksTransaction::consensus_deserialize`) straight from client-controlled JSON, and the `TryFrom` impl copies every field verbatim into `SignerEvent::NewBlock` [3](#0-2) .

The intended invariant is `NewBlock.transactions == transactions actually mined in block_id`, i.e., this event is supposed to represent the node's authoritative announcement of a processed block. That equality is broken here: nothing ties this HTTP request to the node process — `bind()` just opens a `tiny_http::Server` socket with no mTLS, shared secret, or source-IP allow-list [4](#0-3) . The `auth_token`/`auth_password` mechanism referenced in the docs and configs protects the *node's RPC* endpoint (proposal submission), not this signer-side event listener, which has no matching authentication field at all [5](#0-4) .

This gap is explicitly acknowledged in code: `SpawnedSigner::new` logs a warning that "the signer is primarily designed for use with a local or subnet network stacks node" and cautions about exposing it to external senders without additional security checks [6](#0-5) . Sample configs bind the signer's listen endpoint to `0.0.0.0:30000`, i.e., all interfaces, not localhost-only [7](#0-6) .

Once forwarded, `SignerEvent::NewBlock` is consumed by `Signer::handle_event_match` in `stacks-signer/src/v0/signer.rs`, which calls `stacks_block_arrival(...)` and, if a matching block is already tracked in `signer_db` in a non-`GloballyAccepted` state, marks it `mark_globally_accepted()` and persists that state [8](#0-7) . The design explicitly treats a `NewBlock` announcement as ground truth for global acceptance, without re-deriving acceptance from signatures [9](#0-8) .

### Impact Explanation
The attacker cannot fabricate acceptance of an unrelated real block (the local-state-machine transition still requires a matching entry keyed by `signer_sighash`/`block_id` in `signer_db`, which the signer itself populated from validated proposals), but this is nonetheless an unauthenticated write path into the signer's runtime pipeline and local state machine: it can inject transactions/hashes/heights that were never mined, corrupt `recently_processed`, and desynchronize the signer's local view of the chain relative to reality with a single crafted POST, repeatable at will. This matches an "unauthenticated write to state" class of issue rather than remote code execution or consensus forgery, since downstream consumption is gated by existing `signer_db` lookups.

### Likelihood Explanation
This requires only network reachability to the signer's event-receiver port (which sample configs default to binding on `0.0.0.0`, i.e., all interfaces) and requires no secret, no valid StackerDB slot, and no privileged role — squarely within the "unprivileged remote attacker" threat model. The maintainers are aware of and have explicitly documented this exposure risk, recommending the signer only be run against a local/trusted node, which indicates this is a known, accepted operational constraint rather than a hidden defect — but as coded, the guard against forged events is entirely absent from the software itself.

### Recommendation
Require authentication on the signer's event-receiver HTTP endpoint (e.g., a shared-secret header/token, analogous to `auth_token`, verified before parsing `/new_block`, `/new_burn_block`, `/stackerdb_chunks`, `/proposal_response` bodies), and/or bind the receiver to loopback by default, rejecting non-local connections unless explicit configuration opts in.

### Proof of Concept
1. Start a `SignerEventReceiver` and bind it to a test socket as in `libsigner`'s existing receiver tests.
2. From a separate connection (simulating an unprivileged remote sender), hex-encode a locally constructed `StacksTransaction` and POST a JSON body to `/new_block` with attacker-chosen `index_block_hash`, `consensus_hash`, `block_hash`, `block_height`, and `"transactions": ["0x<raw_tx hex>"]`.
3. Call `next_event()` and assert the returned `SignerEvent::NewBlock { transactions, .. }` contains exactly the attacker-supplied transaction (matching `StacksTransaction::consensus_deserialize` output), demonstrating no verification of request origin occurred at `process_event::<T, StacksBlockEvent>` in `libsigner/src/events.rs`.

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

**File:** libsigner/src/events.rs (L446-447)
```rust
            } else if request.url() == "/new_block" {
                process_event::<T, StacksBlockEvent>(request)
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

**File:** stacks-signer/src/config.rs (L320-327)
```rust
    /// The authorization password for the block proposal endpoint.
    /// ---
    /// @default: (required, no default)
    /// @notes:
    ///   - WARNING: Must match the `auth_token` in the Stacks node's
    ///     `[connection_options]` section. If these do not match, the signer
    ///     cannot communicate with the node.
    pub auth_password: String,
```

**File:** stacks-signer/src/lib.rs (L124-132)
```rust
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** stacks-signer/src/v0/signer.rs (L706-726)
```rust
                self.local_state_machine
                    .stacks_block_arrival(consensus_hash, *block_height, block_id, signer_sighash, &self.signer_db, transactions)
                    .unwrap_or_else(|e| error!("{self}: failed to update local state machine for latest stacks block arrival"; "err" => ?e));

                if let Ok(Some(mut block_info)) = self
                    .signer_db
                    .block_lookup(signer_sighash)
                    .inspect_err(|e| warn!("{self}: Failed to load block state: {e:?}"))
                {
                    if block_info.state == BlockState::GloballyAccepted {
                        // We have already globally accepted this block. Do nothing.
                        return;
                    }
                    if let Err(e) = block_info.mark_globally_accepted() {
                        warn!("{self}: Failed to mark block as globally accepted: {e:?}");
                        return;
                    }
                    if let Err(e) = self.signer_db.insert_block(&block_info) {
                        warn!("{self}: Failed to update block state to globally accepted: {e:?}");
                    }
                }
```

**File:** docs/signer-flows.md (L114-118)
```markdown
A `NewBlock` event is the node announcing a processed block. Global acceptance
is never derived from counting signatures: it is marked either here, when the
node announces the block, or in `check_latest_block_in_tenure` (section 7), when
the node reports the block as the processed tip of its tenure. Seeing the chain
adopt the block is the ground truth.
```
