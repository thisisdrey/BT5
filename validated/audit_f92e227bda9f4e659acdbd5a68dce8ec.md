### Title
Single out-of-range `slot_id` StackerDB chunk permanently kills `SignerCoordinator`'s listener thread - ([File: stacks-node/src/nakamoto_node/stackerdb_listener.rs])

### Summary
`StackerDBListener::run` hard-fails with `Err(NakamotoNodeError::SignerSignatureError(...))` the moment it encounters a signer message whose `slot_id` is not a key in its locally cached `signer_entries` map. Since the listener thread is spawned once in `SignerCoordinator::new` and any `Err` from `listener.run()` is only logged (never retried or respawned), a single such message permanently disables the miner's signer-coordination path for that tenure.

### Finding Description
In `StackerDBListener::run`, for every message pulled from a StackerDB chunk event, the code does: [1](#0-0) 
This is a hard `return Err(...)` — not a `continue` — unlike every other malformed/unexpected-data case in the same loop (e.g. unknown block hash, bad signature, wrong signer_set), which are all handled with `continue`/`warn!`/`info!` and never abort the loop.

`signer_entries` is built once at `StackerDBListener::new` time from `reward_set.signers()`, indexed by `enumerate()` position, and is never refreshed for the lifetime of the listener: [2](#0-1) 

Messages are filtered only by `signer_set` (reward-cycle parity, 0 or 1), not by exact reward-cycle id or contract identity: [3](#0-2) 

The `.signers-0`/`.signers-1` StackerDB contracts are reused across alternating reward cycles. If the current listener's cached `signer_entries` (built for the currently active reward cycle) has fewer signer slots than what a legitimately-signing slot owner is authorized to write to (e.g. residual writes tied to the alternating contract's slot layout, or any divergence between the locally rebuilt `enumerate()`-based map and the actual configured slot assignment), a valid, correctly-signed chunk write to a slot index outside the cached map's key range reaches this code path and triggers the early `Err` return.

Once `run()` returns `Err`, the spawning closure in `SignerCoordinator::new` only logs it: [4](#0-3) 
There is no retry, no thread respawn, and `SignerCoordinator::shutdown` only joins the already-dead thread. All subsequent calls that rely on `stackerdb_comms` (block signature collection, tenure-extend timestamps, global-state evaluation) silently stop receiving updates because the underlying producer thread is gone — the miner will keep proposing blocks but can never observe further signer responses through this channel, and `wait_for_block_status` will just keep timing out.

### Impact Explanation
A single StackerDB chunk with an out-of-range `slot_id` permanently terminates the `StackerDBListener` thread. From that point, `SignerCoordinator`'s in-memory view of signer approvals/rejections/timestamps stops updating for the remainder of the miner's tenure operation, denying the block-signing coordination path with one message and requiring no further interaction — matching a Critical unauthenticated-DoS category since the trigger is a single wire message that kills a long-lived thread with no self-healing.

### Likelihood Explanation
The attacker model allows an actor who legitimately owns some StackerDB slot to gossip messages; exploiting this requires only that such a legitimate writer target a `slot_id` outside the locally-cached `signer_entries` window (e.g. across a reward-cycle contract-reuse boundary, or any drift between the actual configured slot assignment and the `enumerate()`-rebuilt map in `StackerDBListener::new`). No RPC secret, no privileged role, and no volumetric traffic is required — one correctly-signed chunk suffices, and the effect is deterministic and repeatable per new `SignerCoordinator` instance.

### Recommendation
Change the `None` branch at `stackerdb_listener.rs:373-377` to log-and-`continue` (mirroring the treatment of every other malformed-message case in this loop) instead of returning `Err` and killing the thread. Additionally, consider having `SignerCoordinator` respawn `StackerDBListener` on unexpected `Err` exits rather than only logging them.

### Proof of Concept
Rust unit test in `stackerdb_listener.rs`'s test module (or a new test file) plan:
1. Construct a `StackerDBListener` via `StackerDBListener::new` with a `RewardSet` containing e.g. 2 signer entries (`signer_entries` keys `{0,1}`).
2. Build a `StackerDBChunksEvent` for the `.signers-<n>` contract matching `self.signer_set`, whose `modified_slots` / decoded `SignerEvent::SignerMessages` contains a tuple `(slot_id = 99, pubkey, SignerMessageV0::BlockResponse(...))`.
3. Push this event into the listener's channel and call `listener.run()` in a loop iteration (or refactor the per-message body into a testable helper).
4. Assert the call returns `Err(NakamotoNodeError::SignerSignatureError(_))` at line 373-377, and that a wrapping thread (as spawned in `SignerCoordinator::new`) exits after logging, with `stackerdb_comms` afterward never receiving further updates (e.g. `wait_for_block_status` times out indefinitely on a freshly inserted block).

### Citations

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L227-239)
```rust
        let signer_entries = reward_set_signers
            .iter()
            .cloned()
            .enumerate()
            .map(|(idx, signer)| {
                let Ok(slot_id) = u32::try_from(idx) else {
                    return Err(ChainstateError::InvalidStacksBlock(
                        "Signer index exceeds u32".into(),
                    ));
                };
                Ok((slot_id, signer))
            })
            .collect::<Result<HashMap<_, _>, ChainstateError>>()?;
```

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L331-361)
```rust
            // check to see if this event we got is a signer event
            let is_signer_event =
                event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot();

            if !is_signer_event {
                debug!("StackerDBListener: Ignoring StackerDB event for non-signer contract"; "contract" => %event.contract_id);
                continue;
            }

            let modified_slots = &event.modified_slots.clone();

            let Ok(signer_event) = SignerEvent::<SignerMessageV0>::try_from(event).map_err(|e| {
                warn!("StackerDBListener: Failure parsing StackerDB event into signer event. Ignoring message."; "err" => ?e);
            }) else {
                continue;
            };
            let SignerEvent::SignerMessages {
                signer_set,
                messages,
                ..
            } = signer_event
            else {
                debug!("StackerDBListener: Received signer event other than a signer message. Ignoring.");
                continue;
            };
            if signer_set != self.signer_set {
                debug!(
                    "StackerDBListener: Received signer event for other reward cycle. Ignoring."
                );
                continue;
            };
```

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L372-377)
```rust
            for (slot_id, _pk, message) in messages.into_iter() {
                let Some(signer_entry) = &self.signer_entries.get(&slot_id) else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Signer entry not found".into(),
                    ));
                };
```

**File:** stacks-node/src/nakamoto_node/signer_coordinator.rs (L169-182)
```rust
        let listener_thread = std::thread::Builder::new()
            .name(format!(
                "stackerdb_listener_{}",
                election_block.block_height
            ))
            .spawn(move || {
                if let Err(e) = listener.run() {
                    error!("StackerDBListener: exited with error: {e:?}");
                }
            })
            .map_err(|e| {
                error!("Failed to spawn stackerdb_listener thread: {e:?}");
                ChainstateError::MinerAborted
            })?;
```
