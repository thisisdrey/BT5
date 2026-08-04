### Title
Permanent inbound-lane deadlock via unfilterable single-message dispatch weight in `receive_messages_proof` - (File: `bridges/modules/messages/src/lib.rs`)

### Summary
The bridge messages pallet enforces strict, gap-free, in-order nonce delivery on every inbound lane (`InboundLane::receive_message`), and the `receive_messages_proof` extrinsic aborts the *entire* delivery transaction with `Error::InsufficientDispatchWeight` whenever the relayer-declared weight for the next message in nonce order is insufficient — instead of skipping it as the accompanying code comment claims. Because nonces must be delivered strictly in order and there is no permissioned "force-skip" or "purge single nonce" mechanism analogous to `pallet-message-queue`'s overweight-execution path, a single message whose true required dispatch weight is (or becomes) larger than any value a relayer can practically declare within the extrinsic's weight bounds can permanently block that lane, mirroring the "first plugin" lock in the reported Vault bug: one bad unit of work at a fixed queue position stalls everything behind it, with no in-protocol way to route around, remove, or rebalance past it.

### Finding Description
`InboundLane::receive_message` requires messages to be delivered strictly in nonce order: [1](#0-0) 

In `receive_messages_proof`, the pallet iterates the proven messages for a lane and, for each one, checks that the caller declared enough leftover weight to dispatch it; if not, the whole call fails outright: [2](#0-1) 

The in-code comment explicitly states the intended behavior is to *skip to the next lane* ("We can't dispatch lane messages out-of-order, so if declared weight is not enough, let's move to next lane"), but the actual code path calls `fail!(Error::<T, I>::InsufficientDispatchWeight)`, which reverts the complete extrinsic — including any other lanes' progress bundled in the same proof and any already-processed messages in this call. [3](#0-2) 

Because delivery is strictly nonce-ordered (`InvalidNonce` is returned for any nonce that isn't `last_delivered_nonce + 1`), a relayer cannot simply skip the problematic nonce and deliver later ones first — the queue can only ever advance by dispatching messages in order: [4](#0-3) 

Unlike `pallet-message-queue`, which has an explicit "permanently overweight" concept and a manual `execute_overweight` recovery path for stuck items so the ready-ring can advance past them, the bridge messages pallet's inbound lane has no equivalent permissionless or governance-triggered mechanism to force-advance past a nonce whose declared/real dispatch weight requirement cannot be satisfied within a single extrinsic's weight budget. The `T::MessageDispatch::dispatch_weight` value that decides this is attacker/sender-influenced (it is derived from the bridged chain's message payload, i.e., untrusted input to the target chain), so the message's weight requirement is effectively set by whichever party crafted the source-chain XCM/call bundled into the message, not the target-chain relayer or governance.

### Impact Explanation
If a message's declared dispatch weight requirement exceeds what any relayer can supply while staying within the block/extrinsic weight limit (e.g., due to a maliciously or accidentally heavy bridged XCM program), that message's nonce can never be delivered. Since delivery is strictly sequential per lane, every subsequent nonce on that lane becomes permanently undeliverable as well — this stalls all message-based bridge traffic (asset transfers, XCM programs, governance calls routed over that lane) for that lane indefinitely, a bridge-state lock consistent with the "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund or bridge-state lock" impact classes in scope.

### Likelihood Explanation
Exploitability depends on whether any configured chain in the repository allows a single message's dispatch weight to exceed the deliverable-in-one-extrinsic bound and whether outbound-side limits (e.g., `maximal_incoming_message_size`/weight caps enforced at `send_message` time on the source chain, seen referenced in `bridges/primitives/messages/src/target_chain.rs` and `bridges/bin/runtime-common/src/integrity.rs`) always prevent this. I was not able to fully confirm within the available tool budget whether current runtime configurations (e.g., BridgeHub configs) enforce a hard cap on the maximum weight a single bridged message may declare such that it is always ≤ the maximum deliverable weight per extrinsic. This is the key remaining uncertainty: if such a cap is always enforced and is provably ≤ the max weight assignable in one `receive_messages_proof` call, the deadlock scenario cannot be triggered and this reduces to a documentation/comment inconsistency bug rather than an exploitable stall. This should be verified against the concrete `BridgedChain` weight constants and the `verify_and_decode_messages_proof`/message size-weight bound enforcement before treating this as fully confirmed.

### Recommendation
- Align code behavior with the documented intent: on `InsufficientDispatchWeight` for a given message, do not abort the whole extrinsic — instead stop processing further messages on that lane in the current call while still committing progress already made, allowing the relayer to retry with a larger declared weight in a subsequent transaction (as long as such a larger weight can exist within protocol bounds).
- Enforce (and verify in `integrity_test`/`do_integrity_test`) a hard invariant that any single message's maximum possible dispatch weight, as bounded at the outbound/source side, is provably ≤ the maximum weight that can be declared/spent in a single `receive_messages_proof` call on the inbound/target side, for every configured `BridgedChain`.
- Add a permissioned/permissionless recovery mechanism analogous to `pallet-message-queue::execute_overweight`, allowing a lane's stuck nonce to be dispatched (or explicitly and safely dropped with reward/penalty accounting) outside the normal weight-limited flow if the invariant above is ever violated by configuration or migration error.

### Proof of Concept
Conceptual (config-dependent — requires confirming the weight-cap invariant above does not hold for a given deployment):
1. Source chain sends a message via `send_message()` whose XCM/call payload is crafted (or a bug allows) to require a dispatch weight `W` on the target chain that exceeds the maximum weight any single `receive_messages_proof` extrinsic can declare (bounded by `BlockWeights::max_extrinsic_weight` minus base overhead).
2. This message is assigned nonce `N` on the lane.
3. Any relayer attempts `receive_messages_proof` including nonce `N`; the check at `bridges/modules/messages/src/lib.rs:294` (`message_dispatch_weight.any_gt(dispatch_weight_left)`) fails for every possible value of `dispatch_weight_left` up to the extrinsic's cap, so the call always hits `fail!(Error::InsufficientDispatchWeight)`.
4. Because `InboundLane::receive_message` rejects any nonce other than `last_delivered_nonce + 1` (`ReceptionResult::InvalidNonce`), nonce `N+1, N+2, ...` can never be delivered either.
5. The lane is now permanently stalled with no in-protocol recovery path, matching the "malfunctioning component blocks the whole flow with no way to remove/rebalance" pattern from the source report.

### Citations

**File:** bridges/modules/messages/src/inbound_lane.rs (L186-196)
```rust
	pub fn receive_message<Dispatch: MessageDispatch<LaneId = S::LaneId>>(
		&mut self,
		relayer_at_bridged_chain: &S::Relayer,
		nonce: MessageNonce,
		message_data: DispatchMessageData<Dispatch::DispatchPayload>,
	) -> ReceptionResult<Dispatch::DispatchLevelResult> {
		let mut data = self.storage.data();
		if Some(nonce) != data.last_delivered_nonce().checked_add(1) {
			return ReceptionResult::InvalidNonce;
		}

```

**File:** bridges/modules/messages/src/lib.rs (L284-304)
```rust
			let mut messages_received_status =
				ReceivedMessages::new(lane_id, Vec::with_capacity(lane_data.messages.len()));
			for mut message in lane_data.messages {
				debug_assert_eq!(message.key.lane_id, lane_id);
				total_messages += 1;

				// ensure that relayer has declared enough weight for dispatching next message
				// on this lane. We can't dispatch lane messages out-of-order, so if declared
				// weight is not enough, let's move to next lane
				let message_dispatch_weight = T::MessageDispatch::dispatch_weight(&mut message);
				if message_dispatch_weight.any_gt(dispatch_weight_left) {
					tracing::trace!(
						target: LOG_TARGET,
						?lane_id,
						declared=%message_dispatch_weight,
						left=%dispatch_weight_left,
						"Cannot dispatch any more messages"
					);

					fail!(Error::<T, I>::InsufficientDispatchWeight);
				}
```
