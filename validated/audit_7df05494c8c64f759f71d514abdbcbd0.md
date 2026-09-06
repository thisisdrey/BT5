### Title
Obsolete pre-Nakamoto `Blocks`/`Microblocks` message types bypass the drop guard when routed through `handle_unsolicited_stacks_message` - ([File: stackslib/src/net/unsolicited.rs])

### Summary
`handle_unsolicited_sortition_message` explicitly drops obsolete epoch2x message variants (`Blocks`, `Microblocks`, `BlocksAvailable`, `MicroblocksAvailable`) by returning `(false, false)`, but the tenure-bound sibling function `handle_unsolicited_stacks_message` has no such guard and falls through its `_ => (false, true)` catch-all for these exact same payload types. Because the "not-relayed/not-buffered" messages returned by the sortition-bound handler are fed directly into the tenure-bound handler (per the function's own doc comment), an obsolete message that was nominally "dropped" at the sortition layer is re-evaluated at the tenure layer and comes back with `relay = true`.

### Finding Description
`handle_unsolicited_sortition_message` (stackslib/src/net/unsolicited.rs:482-520) matches `StacksMessageType::BlocksAvailable`, `MicroblocksAvailable`, `Blocks`, and `Microblocks` and intentionally returns `(false, false)` to prevent these obsolete epoch2x types from being relayed in the Nakamoto era. However, the enclosing retain loop in `handle_unsolicited_sortition_messages` (unsolicited.rs:646-696) discards the `relay` component of that tuple for the purposes of keeping/dropping the message from the returned map — it only uses `relay` for a debug log statement (line 683-691) and only removes a message from the retained set when it was actually buffered (`buffer && to_buffer`, line 679-682). Every other message, including ones with `relay = false`, is kept (`true` at line 692) in the map that this function returns.

That returned map is explicitly documented (unsolicited.rs:637-638) to "be fed directly into `handle_unsolicited_stacks_messages()`," which is the tenure-bound counterpart. `handle_unsolicited_stacks_message` (unsolicited.rs:539-577) only special-cases `StackerDBPushChunk`; every other payload type — including the very `Blocks`/`Microblocks`/`BlocksAvailable`/`MicroblocksAvailable` variants that were supposedly dropped one layer up — falls into `_ => (false, true)`, meaning `relay` is now `true`. The claimed equality ("obsolete type dropped at one layer == dropped at every layer it can reach") is broken: the drop guard exists only in the sortition-bound function and is not mirrored in the tenure-bound function, even though the tenure-bound function is reachable with these same obsolete payloads. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
An unprivileged remote peer that is authenticated (i.e., has completed a normal handshake, which any connecting peer can do) can send an unsolicited `Blocks`/`Microblocks`/`BlocksAvailable`/`MicroblocksAvailable` message. Directly calling `handle_unsolicited_stacks_message` with such a payload returns `relay = true`, contradicting the codebase's stated intent (documented at the top of the file) that these legacy types "are dropped on receipt since they are obsolete in the Nakamoto era." I was not able to fully trace, within the remaining tool budget, what the ultimate downstream consumer of the "relay=true" / retained-message map does with a resurrected `Blocks` payload inside `stackslib/src/net/p2p.rs` (i.e., whether any epoch2x block-storage/processing code path is still reachable in Nakamoto mode to act on it, or whether it is inert once it reaches the relayer). That downstream step determines whether this manifests only as a dead/inert flag mismatch versus an actual forged-data-processing path, so I cannot confirm the full "Critical" severity claimed in the question with certainty — I can only confirm the exact match-arm/logic defect itself with line-level evidence.

### Likelihood Explanation
The precondition is only a completed, unprivileged peer handshake (authentication check at unsolicited.rs:56-80 passes for any legitimately-connected peer), after which the attacker can send a single crafted `StacksMessageType::Blocks` (or `Microblocks`/`*Available`) message. No secrets, admin roles, or privileged state are required, and the message can be repeated arbitrarily.

### Recommendation
Mirror the explicit drop guard from `handle_unsolicited_sortition_message` inside `handle_unsolicited_stacks_message`, matching `StacksMessageType::BlocksAvailable | MicroblocksAvailable | Blocks | Microblocks` and returning `(false, false)` there as well, so that obsolete epoch2x message types cannot acquire `relay = true` regardless of which unsolicited-handling path they are funneled through.

### Proof of Concept
In `stackslib/src/net/unsolicited.rs` (or a new test module under `stackslib/src/net/tests/`), construct a `StacksMessage` with `payload: StacksMessageType::Blocks(BlocksData { blocks: vec![] })` (or any minimal valid `BlocksData`), obtain a `PeerNetwork` and `StacksChainState` test fixture (as used by existing tests in `stackslib/src/net/tests/relay/nakamoto.rs`), and call:
```rust
let (to_buffer, relay) = peer_network.handle_unsolicited_stacks_message(
    &mut chainstate,
    event_id,
    &message.preamble,
    &message.payload,
    /* buffer = */ false,
);
assert_eq!(relay, false, "obsolete Blocks message must not be relayed");
```
The assertion fails because the current `_ => (false, true)` catch-all at unsolicited.rs:575 returns `relay = true` for `StacksMessageType::Blocks`.

### Citations

**File:** stackslib/src/net/unsolicited.rs (L490-505)
```rust
        match payload {
            // Explicitly match obsolete Stacks v2 messages.
            // Although they could fall under `_` match, listing them here documents
            // that they are intentionally dropped. Remove once support for
            // these messages is removed from the codebase.
            StacksMessageType::BlocksAvailable(_)
            | StacksMessageType::MicroblocksAvailable(_)
            | StacksMessageType::Blocks(_)
            | StacksMessageType::Microblocks(_) => {
                debug!(
                    "{:?}: Drop obsolete pre-Nakamoto message: {}",
                    self.get_local_peer(),
                    payload.get_message_description()
                );
                (false, false)
            }
```

**File:** stackslib/src/net/unsolicited.rs (L547-577)
```rust
        match payload {
            StacksMessageType::StackerDBPushChunk(ref data) => {
                // N.B. send back a reply if we're calling to buffer, since this would be the first
                // time we're seeing this message (instead of a subsequent time on follow-up
                // processing).
                let (can_buffer, can_store) = self
                    .handle_unsolicited_StackerDBPushChunk(
                        chainstate, event_id, preamble, data, buffer,
                    )
                    .unwrap_or_else(|e| {
                        info!(
                            "{:?}: failed to handle unsolicited {:?} when buffer = {}: {:?}",
                            self.get_local_peer(),
                            payload,
                            buffer,
                            &e
                        );
                        (false, false)
                    });
                if buffer && can_buffer && !can_store {
                    debug!(
                        "{:?}: Buffering {:?} to retry on next sortition",
                        self.get_local_peer(),
                        &payload
                    );
                }
                (can_buffer, can_store)
            }
            _ => (false, true),
        }
    }
```

**File:** stackslib/src/net/unsolicited.rs (L672-693)
```rust
                let (to_buffer, relay) = self.handle_unsolicited_sortition_message(
                    sortdb,
                    chainstate,
                    *event_id,
                    &message.payload,
                    buffer,
                );
                if buffer && to_buffer {
                    self.buffer_sortition_data_message(*event_id, neighbor_key, message.clone());
                    return false;
                }
                if relay {
                    // forward to relayer for processing
                    debug!(
                        "{:?}: Will forward message {} from {:?} to relayer",
                        &self.get_local_peer(),
                        &message.payload.get_message_description(),
                        &neighbor_key
                    );
                }
                true
            });
```
