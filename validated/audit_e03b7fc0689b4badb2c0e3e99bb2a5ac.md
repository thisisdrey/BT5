### Title
Unauthenticated `/new_block` event endpoint lets a remote attacker forge global block acceptance and pollute the signer's replicated state machine - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event()` at `libsigner/src/events.rs:413-459` accepts any POST to `/new_block` on the signer's event-receiver socket and deserializes it into a `StacksBlockEvent` with no shared-secret, source-IP, or signature check anywhere in the code path. The resulting `SignerEvent::NewBlock` is fed directly into `Signer::handle_event_match` (`stacks-signer/src/v0/signer.rs:675-727`), which uses the attacker-supplied `signer_sighash`, `consensus_hash`, `block_id`, `block_height`, and `transactions` to update `LocalStateMachine` and, if the sighash matches a block the signer already tracks, unconditionally marks that block `BlockState::GloballyAccepted`.

### Finding Description
The claimed broken equality — "no shared secret is checked between the node and its signer event-receiver" — holds: `SignerEventReceiver::bind()` (`libsigner/src/events.rs:404-408`) simply calls `HttpServer::http(listener)` with no auth middleware, and `next_event()`/`process_event()` (`libsigner/src/events.rs:413-459`, `519-542`) dispatch purely on `request.url()` and JSON-decode the body with no signature, HMAC, or password check. The only "secret" (`auth_password`/`auth_token`) documented in `docs/signing.md` and `sample/conf/signer/mainnet-signer-conf.toml` governs the signer→node RPC (block-proposal validation) direction, not the node→signer event push handled here.

The default sample config binds the signer's event endpoint to `0.0.0.0:30000` (`sample/conf/signer/mainnet-signer-conf.toml:39`), making the port remotely reachable, matching the attacker precondition of "any TCP source."

Exploit flow:
1. Attacker connects to the signer's event port and sends `POST /new_block` with a JSON body deserializing into `StacksBlockEvent` (`libsigner/src/events.rs:690-706`), supplying an arbitrary `index_block_hash`, `consensus_hash`, `block_hash`, `block_height`, `transactions`, and, crucially, a `signer_signature_hash` equal to a `signer_sighash` that the target signer is currently tracking as `Unprocessed`/`PreCommitted`/`LocallyAccepted` (these sighashes are visible on the wire via StackerDB/gossip, so the attacker can learn a valid, in-flight one without any privileged access).
2. `process_event::<T, StacksBlockEvent>` (`libsigner/src/events.rs:519-542`) converts this into `SignerEvent::NewBlock` (`libsigner/src/events.rs:708-720`) with zero validation of any field against actual node state.
3. `Signer::handle_event_match` (`stacks-signer/src/v0/signer.rs:675-727`) calls `local_state_machine.stacks_block_arrival(consensus_hash, block_height, block_id, signer_sighash, ..., transactions)` — feeding attacker-controlled `consensus_hash`/`block_height`/`block_id`/`transactions` into the miner-state update logic (`stacks-signer/src/v0/signer_state.rs:454+`), which can update `parent_tenure_last_block`/`parent_tenure_last_block_height` in the locally-held state machine.
4. It then does `signer_db.block_lookup(signer_sighash)` and, if found and not already `GloballyAccepted`, calls `block_info.mark_globally_accepted()` and `signer_db.insert_block(&block_info)` (`stacks-signer/src/v0/signer.rs:710-726`) — writing `BlockState::GloballyAccepted` purely because a forged HTTP POST said so, without the actual stacks-node having processed or announced that block.
5. Per `docs/signer-flows.md:114-118`: "Global acceptance is never derived from counting signatures: it is marked either here [on `NewBlock`], ... Seeing the chain adopt the block is the ground truth." This "ground truth" signal is exactly what is unauthenticated here — the node-to-signer channel that is supposed to be trusted as canonical-tip confirmation has no cryptographic binding to the real node at all.

Existing guards that do NOT prevent this: `block_lookup` only requires that some sighash already exist in the local signerdb (from an earlier, legitimately-received proposal or gossiped message) — it does not verify the accompanying fields (`consensus_hash`, `block_id`, `transactions`, `block_height`) came from the real node, nor does it re-validate the block against the node's RPC.

### Impact Explanation
- A remote, unprivileged attacker who can merely open a TCP connection to the signer's event port can force a legitimately-proposed-but-not-yet-confirmed block into `BlockState::GloballyAccepted` in the signer's local `SignerDb`, which feeds `get_canonical_tip`, `get_last_globally_accepted_block`, and the state-machine `parent_tenure_last_block` used for subsequent proposal/tenure-conflict checks (`stacks-signer/src/chainstate/mod.rs`).
- This is state served/committed as canonical (`BlockState::GloballyAccepted`) without the node having actually processed or announced it — matching the "High: serving non-canonical state as canonical" category, and arguably "Critical: unauthenticated/unauthorized write to state," since it is a direct unauthenticated write into signer-persisted chainstate that other logic treats as ground truth.
- The corrupted `local_state_machine` (`parent_tenure_last_block`/`parent_tenure_last_block_height`) can also be broadcast to other signers via `send_signer_update_message` (StateMachineUpdate over StackerDB) when state changes, which is a path toward network-wide propagation of a poisoned view, though full corroboration of that final broadcast step under all conditions was not exhaustively traced in the time available.
- Repeatable: attacker can resend this for every block the signer is currently tracking, as long as it can learn/guess the `signer_signature_hash`.

### Likelihood Explanation
- No privileged role, no shared secret, and no local access are required — only network reachability to the signer's event port, which per the sample configs (`sample/conf/signer/mainnet-signer-conf.toml:39`) can be `0.0.0.0:<port>`.
- The attacker needs to know a `signer_signature_hash` currently tracked by the target signer; this is realistically obtainable since block proposals and pre-commit/response messages circulate over StackerDB/gossip and are not secret.
- Cost is a single crafted HTTP POST; fully repeatable for each block in flight.

### Recommendation
Add authentication to the node→signer event channel (e.g., require the same/its own shared secret or a signed payload from the node, verified in `next_event`/`process_event` before trusting `StacksBlockEvent` fields), and/or cryptographically bind `NewBlock` events to the node's actual chain state (e.g., re-verify against the node's `/v2/info` or tenure-tip RPC before calling `mark_globally_accepted`), rather than trusting an unauthenticated HTTP POST as sufficient proof of global acceptance.

### Proof of Concept
```rust
// Pseudocode Rust test plan (net test) for libsigner/src/events.rs
// 1. Spawn a SignerEventReceiver bound to 127.0.0.1:0, get local_addr.
// 2. Insert a BlockInfo into a SignerDb with state=Unprocessed for a known
//    signer_signature_hash `H` (simulating a real earlier proposal).
// 3. Open a raw TcpStream to local_addr from the test itself (no auth headers).
// 4. Send:
//    POST /new_block HTTP/1.1
//    Content-Length: <n>
//
//    {"index_block_hash":"0xAA..","consensus_hash":"0xBB..","block_hash":"0xCC..",
//     "block_height":1,"signer_signature_hash":"0x<H>","transactions":[]}
// 5. Call next_event() -> assert Ok(SignerEvent::NewBlock{ signer_sighash: Some(H), .. }).
// 6. Feed that event through Signer::handle_event_match / process_event and
//    assert signer_db.block_lookup(&H).unwrap().state == BlockState::GloballyAccepted,
//    despite no real node ever having reported this block.
// Assertion failure site to target: `block_info.mark_globally_accepted()` and
// `signer_db.insert_block(&block_info)` in stacks-signer/src/v0/signer.rs:719-724
// executing purely from attacker-supplied HTTP input with no auth check.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** stacks-signer/src/v0/signer.rs (L675-727)
```rust
            SignerEvent::NewBlock {
                block_height,
                block_id,
                consensus_hash,
                signer_sighash,
                transactions,
            } => {
                #[cfg(any(test, feature = "testing"))]
                if self.test_ignore_all_block_announcements(
                    *block_height,
                    block_id,
                    consensus_hash,
                    signer_sighash,
                    transactions,
                ) {
                    return;
                }

                let Some(signer_sighash) = signer_sighash else {
                    debug!("{self}: received a new block event for a pre-nakamoto block, no processing necessary");
                    return;
                };
                self.recently_processed.add_block(block_id.clone());
                info!(
                    "{self}: Received a new block event.";
                    "block_id" => %block_id,
                    "signer_signature_hash" => %signer_sighash,
                    "consensus_hash" => %consensus_hash,
                    "block_height" => block_height,
                    "total_txs" => transactions.len()
                );
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
            }
```

**File:** stacks-signer/src/v0/signer_state.rs (L453-540)
```rust
    /// Handle a new stacks block arrival
    pub fn stacks_block_arrival(
        &mut self,
        ch: &ConsensusHash,
        height: u64,
        block_id: &StacksBlockId,
        signer_signature_hash: &Sha512Trunc256Sum,
        db: &SignerDb,
        txs: &Vec<StacksTransaction>,
    ) -> Result<(), SignerChainstateError> {
        // set self to uninitialized so that if this function errors,
        //  self is left as uninitialized.
        let prior_state = std::mem::replace(self, Self::Uninitialized);
        let mut prior_state_machine = match prior_state {
            // if the local state machine was uninitialized, just initialize it
            LocalStateMachine::Initialized(signer_state_machine) => signer_state_machine,
            LocalStateMachine::Uninitialized => {
                // we don't need to update any state when we're uninitialized for new stacks block
                //  arrivals
                return Ok(());
            }
            LocalStateMachine::Pending { update, prior } => {
                // This works as long as the pending updates are only burn blocks,
                //  but if we have other kinds of pending updates, this logic will need
                //  to be changed.
                match &update {
                    StateMachineUpdate::BurnBlock(..) => {
                        *self = LocalStateMachine::Pending { update, prior };
                        return Ok(());
                    }
                }
            }
        };

        if let Some(replay_set_hash) = NakamotoBlockProposal::tx_replay_hash(
            &prior_state_machine.tx_replay_set.clone_as_optional(),
        ) {
            match db.get_was_block_validated_by_replay_tx(signer_signature_hash, replay_set_hash) {
                Ok(Some(BlockValidatedByReplaySet {
                    replay_tx_exhausted,
                    ..
                })) => {
                    if replay_tx_exhausted {
                        // This block was validated by our current state machine's replay set,
                        // and the block exhausted the replay set. Therefore, clear the tx replay set.
                        info!("Signer State: Incoming Stacks block exhausted the replay set, clearing the tx replay set";
                            "signer_signature_hash" => %signer_signature_hash,
                        );
                        prior_state_machine.tx_replay_set = ReplayTransactionSet::none();
                    }
                }
                Ok(None) => {
                    info!("Signer State: got a new block during replay that wasn't validated by our replay set. Clearing the local replay set.";
                        "txs" => ?txs,
                    );
                    prior_state_machine.tx_replay_set = ReplayTransactionSet::none();
                }
                Err(e) => {
                    warn!("Signer State: Failed to check if block was validated by replay tx";
                        "err" => ?e,
                        "signer_signature_hash" => %signer_signature_hash,
                    );
                }
            }
        }

        let MinerState::ActiveMiner {
            parent_tenure_id,
            parent_tenure_last_block,
            parent_tenure_last_block_height,
            ..
        } = &mut prior_state_machine.current_miner
        else {
            // if there's no valid miner, then we don't need to update any state for new stacks blocks
            *self = LocalStateMachine::Initialized(prior_state_machine);
            return Ok(());
        };

        if parent_tenure_id != ch {
            // if the new block isn't from the parent tenure, we don't need any updates
            *self = LocalStateMachine::Initialized(prior_state_machine);
            return Ok(());
        }

        if height <= *parent_tenure_last_block_height {
            // if the new block isn't higher than we already expected, we don't need any updates
            *self = LocalStateMachine::Initialized(prior_state_machine);
            return Ok(());
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L33-50)
```text
# REQUIRED: The Stacks node RPC endpoint to connect to.
# Must match the node's [node] rpc_bind address.
node_host = "127.0.0.1:20443"

# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"

# REQUIRED: Network selection.
# Valid values: "mainnet", "testnet", "mocknet"
network = "mainnet"

# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```

**File:** docs/signing.md (L24-60)
```markdown

```toml
[node]
stacker = true

[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```

### 2. Configure the Signer

Use [`mainnet-signer-conf.toml`](../sample/conf/signer/mainnet-signer-conf.toml) as a starting point.
Key settings:

```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```

### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |

```
