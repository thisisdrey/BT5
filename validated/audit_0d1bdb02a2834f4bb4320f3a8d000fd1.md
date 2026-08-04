### Title
Inbound bridge lane advances delivered-nonce state only *after* dispatching the message, allowing a nested `Transact` call to re-deliver the same nonce before it is marked delivered - ([File: bridges/modules/messages/src/inbound_lane.rs])

### Summary
`InboundLane::receive_message` in `bridges/modules/messages/src/inbound_lane.rs` reads the lane's delivery state into a local variable, dispatches the incoming message (which can execute an arbitrary `Transact` call via `T::MessageDispatch`), and only afterwards writes the updated `last_delivered_nonce`/`relayers` state back to storage. This is the exact "update accounting after the external interaction" pattern described in the CNote/CErc20 report: the nonce that guards against duplicate delivery is not committed until after the side-effecting dispatch has already run, so a nested call that lands back on the same nonce sees stale state and can be processed a second time before the guard is persisted.

### Finding Description
`receive_message` works like this: [1](#0-0) 

1. `let mut data = self.storage.data();` reads the current `InboundLaneData` (including `last_delivered_nonce()`).
2. It checks `Some(nonce) != data.last_delivered_nonce().checked_add(1)` against that **stale, in-memory** copy.
3. It calls `Dispatch::dispatch(DispatchMessage { .. })` — for bridge-hub runtimes this is `XcmBlobMessageDispatch`, which executes the message as an XCM program. XCM programs can contain a `Transact` instruction, which executes an arbitrary `RuntimeCall` under a derived (but ordinary `Signed`) origin.
4. Only **after** `dispatch()` returns does the function update `data.relayers` and call `self.storage.set_data(data)`, persisting the new `last_delivered_nonce`.

Because FRAME storage writes are visible immediately through the same execution's storage overlay, but here the write is deliberately deferred until step 4, any code that reenters `pallet_bridge_messages::receive_messages_proof` for the **same lane** while step 3 is still running observes the storage exactly as it was before this message's delivery was recorded. The relevant call-site is: [2](#0-1) 

`receive_messages_proof` is a `Signed`-origin, permissionless extrinsic (`ensure_signed(origin)?`); it does not wrap the per-message loop in a `with_transaction`/reentrancy guard, unlike `pallet-revive`'s explicit `ReenteredPallet` protection for `call_runtime`. If the dispatched message's `Transact` payload is itself (or ultimately triggers) another `receive_messages_proof` call for the same lane and the exact nonce currently mid-flight, the nonce check in step 2 above will pass again — `last_delivered_nonce()` has not yet been incremented — and the message will be dispatched a second time before the outer call finally persists the incremented nonce.

This is precisely the broken invariant identified in the external report: state that is supposed to gate re-entry (`accountBorrows`/`last_delivered_nonce`) is written *after* the value-moving external call (`doTransferOut`/`Dispatch::dispatch`), not before, letting a nested/loop-back call observe and act on stale state.

### Impact Explanation
A successfully crafted message causes the same bridged nonce to be dispatched twice within one `receive_messages_proof` call. Since `T::MessageDispatch` for bridge-hub-style runtimes ultimately executes XCM instructions that can mint/unlock/deposit assets (e.g. reserve-based asset transfers, teleports) or execute arbitrary `Transact` calls, duplicate dispatch of the same nonce means duplicate settlement of whatever value-moving effect the message encodes — i.e. unbacked duplication of bridged funds, directly matching the "duplicate settlement or payout" and "message queues ... must only advance after ... settlement succeed atomically" impact categories in scope.

### Likelihood Explanation
The attacker does not need to be a malicious relayer, validator, or governance actor — any account able to originate a message on the bridged chain that is routed through this lane can shape the XCM payload, including a `Transact` instruction. The nonce/lane state check exists specifically to prevent duplicate delivery, but because the state write is deferred past the dispatch call, that guard is bypassable by a same-nonce reentrant call arriving through the dispatched message's own execution, exactly mirroring the reentrancy precondition in the external report (a callback path exists, and accounting is finalized only after the callback returns).

### Recommendation
Persist the updated `InboundLaneData` (incremented `last_delivered_nonce`, updated `relayers`) to storage **before** invoking `Dispatch::dispatch`, or wrap the nonce-check + dispatch + state-update sequence so the nonce is marked delivered atomically prior to dispatch (mirroring `CToken::borrowFresh`'s "write storage before external call" fix). If dispatch fails, the write should still stand (nonce must not be redeliverable), which is consistent with how `MessageDispatch::dispatch` already returns an `unspent_weight`/result rather than reverting storage.

### Proof of Concept
1. Configure a lane whose `T::MessageDispatch` executes XCM messages containing `Transact`, with a `Transact`-derived origin that maps to some `Signed` `AccountId` on this chain (standard `SovereignSignedViaLocation`-style origin conversion).
2. Submit `receive_messages_proof` with a message at nonce `N` whose XCM payload's `Transact` call is itself `pallet_bridge_messages::receive_messages_proof`, submitting a (separately obtained/replayed) valid proof for the *same lane and nonce* `N`.
3. During step 3 of `receive_message` (`Dispatch::dispatch`), the nested call re-enters `InboundLane::receive_message` for nonce `N`; since `self.storage.set_data(data)` from the outer call has not yet executed, `data.last_delivered_nonce()` is still `N-1`, so the nonce check passes and message `N`'s payload (e.g. an asset deposit) is dispatched a second time.
4. The outer call then completes and persists `last_delivered_nonce = N`, masking that the message was actually dispatched twice — resulting in duplicate settlement of the message's value-moving effect.

### Citations

**File:** bridges/modules/messages/src/inbound_lane.rs (L186-229)
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

		// if there are more unrewarded relayer entries than we may accept, reject this message
		if data.relayers.len() as MessageNonce >= self.storage.max_unrewarded_relayer_entries() {
			return ReceptionResult::TooManyUnrewardedRelayers;
		}

		// if there are more unconfirmed messages than we may accept, reject this message
		let unconfirmed_messages_count = nonce.saturating_sub(data.last_confirmed_nonce);
		if unconfirmed_messages_count > self.storage.max_unconfirmed_messages() {
			return ReceptionResult::TooManyUnconfirmedMessages;
		}

		// then, dispatch message
		let dispatch_result = Dispatch::dispatch(DispatchMessage {
			key: MessageKey { lane_id: self.storage.id(), nonce },
			data: message_data,
		});

		// now let's update inbound lane storage
		match data.relayers.back_mut() {
			Some(entry) if entry.relayer == *relayer_at_bridged_chain => {
				entry.messages.note_dispatched_message();
			},
			_ => {
				data.relayers.push_back(UnrewardedRelayer {
					relayer: relayer_at_bridged_chain.clone(),
					messages: DeliveredMessages::new(nonce),
				});
			},
		};
		self.storage.set_data(data);

		ReceptionResult::Dispatched(dispatch_result)
	}
```

**File:** bridges/modules/messages/src/lib.rs (L284-311)
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

				let receival_result = lane.receive_message::<T::MessageDispatch>(
					&relayer_id_at_bridged_chain,
					message.key.nonce,
					message.data,
				);

```
