This confirms the exact mechanism at both delivery-side (target chain, `pallet-bridge-messages`) reward calculation and downstream relayer rewards (`pallet-bridge-relayers`, computed off delivery confirmation), which is a strong local analog to the external report.

### Title
Relayer/Postman is Rewarded for "Dispatched" Messages Even When the Underlying Message Execution Fails - (File: `bridges/modules/messages/src/lib.rs`)

### Summary
`pallet-bridge-messages::receive_messages_proof` pays the relayer ("postman") based on the count of messages that reach `ReceptionResult::Dispatched`, without regard to whether the inner `DispatchLevelResult` actually succeeded. This mirrors the external report's core flaw: a fee/reward is paid based on "the top-level call succeeding" while the "low-level" application-level execution (message dispatch to its real destination/handler) can fail silently, yet the relayer/postman still collects full payment as if delivery was successful.

### Finding Description
In `receive_messages_proof` [1](#0-0) , each message is passed to `lane.receive_message::<T::MessageDispatch>()`, which unconditionally dispatches the message and returns `ReceptionResult::Dispatched(dispatch_result)` "regardless of whether dispatch has been successful or not" as explicitly documented: [2](#0-1) .

The pallet only distinguishes `Dispatched` from `InvalidNonce`/`TooManyUnrewardedRelayers`/`TooManyUnconfirmedMessages` when incrementing `valid_messages`, treating *any* `Dispatched` outcome — success or application-level failure — as a "valid" (i.e., rewarded) message delivery: [3](#0-2) .

`valid_messages` is then handed directly to `T::DeliveryPayments::pay_reward(relayer_id_at_this_chain, total_messages, valid_messages, actual_weight)`, which is the trigger for relayer compensation: [4](#0-3) .

Concretely, when the configured `MessageDispatch` is the XCM blob dispatcher (`pallet-xcm-bridge-hub`), a message whose payload fails to execute at the target (e.g. `DispatchBlob::dispatch_blob` returns an error) still produces `XcmBlobMessageDispatchResult::NotDispatched(Some(e))`, which is still wrapped in `ReceptionResult::Dispatched(...)` and thus still counted as `valid_messages`: [5](#0-4) .

Downstream, `pallet-bridge-relayers`'s signed extension computes relayer refunds/rewards from the same "successful call" signal (`check_call_result`) rather than from confirmation that the dispatched payload was correctly executed at its ultimate destination: [6](#0-5) .

This is structurally identical to the reported bug class: the "postman" (relayer) is compensated once the outer operation (message receipt/dispatch attempt) completes, without a binding, gas/weight-based guarantee that the nested execution (the actual payload delivery/effect) succeeded — the "low-level call" can fail silently while the "top-level call" (and the fee claim tied to it) succeeds.

### Impact Explanation
A relayer can submit message batches, force lane messages into `NotDispatched`/failed states at the application layer (e.g. by choosing timing, congestion, or malformed-but-decodable payload edge cases that make the blob dispatcher fail after acceptance), and still be fully rewarded as if genuine delivery occurred. Over many messages/lanes, this degrades the intended cross-chain delivery guarantee (paying only for effective delivery) and lets relayers extract fees disproportionate to the value actually delivered, without needing any privileged role — only a signed extrinsic call.

### Likelihood Explanation
Any signed account can call `receive_messages_proof`/act as relayer; no admin, governance, or validator collusion is required. The dispatch-vs-execution-success gap is a designed behavior ("we don't care whether dispatch has been successful or not"), so the condition for reward without full correct execution is trivially reachable in normal operation, not just as an edge case.

### Recommendation
Separate the "message was structurally acceptable and consumed" reward tier from a "payload executed successfully at destination" tier, similar to the external report's suggestion of binding gas/weight demanded by the sender to the fee released to the postman. Concretely: incorporate `DispatchLevelResult` (e.g. `XcmBlobMessageDispatchResult::Dispatched` vs `NotDispatched`) into the reward computation in `pay_reward`/`register_relayer_reward`, so relayers are only rewarded (or rewarded at a reduced rate) when the dispatch actually succeeded, not merely attempted.

### Proof of Concept
1. Configure a lane using `pallet-xcm-bridge-hub`'s `MessageDispatch` implementation as `T::MessageDispatch`.
2. Craft/deliver a message whose XCM blob payload is well-formed enough to pass proof verification and nonce checks, but causes `T::BlobDispatcher::dispatch_blob` to return `Err(e)` (e.g., destination channel congested/unroutable at that block) — see `dispatch()` in `bridges/modules/xcm-bridge-hub/src/dispatcher.rs` lines 88-129.
3. Submit this message via `receive_messages_proof`. The lane records `ReceptionResult::Dispatched(MessageDispatchResult{ dispatch_level_result: NotDispatched(Some(e)), .. })`, and `valid_messages` is incremented at `bridges/modules/messages/src/lib.rs` line 320.
4. `T::DeliveryPayments::pay_reward` (or, on the source chain, `pallet_bridge_relayers::RelayerRewards`/`register_relayer_reward` via the delivery-confirmation flow) credits the relayer for this "valid" message even though the XCM payload was never actually dispatched to its destination.

### Citations

**File:** bridges/modules/messages/src/lib.rs (L306-332)
```rust
				let receival_result = lane.receive_message::<T::MessageDispatch>(
					&relayer_id_at_bridged_chain,
					message.key.nonce,
					message.data,
				);

				// note that we're returning unspent weight to relayer even if message has been
				// rejected by the lane. This allows relayers to submit spam transactions with
				// e.g. the same set of already delivered messages over and over again, without
				// losing funds for messages dispatch. But keep in mind that relayer pays base
				// delivery transaction cost anyway. And base cost covers everything except
				// dispatch, so we have a balance here.
				let unspent_weight = match &receival_result {
					ReceptionResult::Dispatched(dispatch_result) => {
						valid_messages += 1;
						dispatch_result.unspent_weight
					},
					ReceptionResult::InvalidNonce |
					ReceptionResult::TooManyUnrewardedRelayers |
					ReceptionResult::TooManyUnconfirmedMessages => message_dispatch_weight,
				};
				messages_received_status.push(message.key.nonce, receival_result);

				let unspent_weight = unspent_weight.min(message_dispatch_weight);
				dispatch_weight_left -= message_dispatch_weight - unspent_weight;
				actual_weight = actual_weight.saturating_sub(unspent_weight);
			}
```

**File:** bridges/modules/messages/src/lib.rs (L335-340)
```rust
			T::DeliveryPayments::pay_reward(
				relayer_id_at_this_chain,
				total_messages,
				valid_messages,
				actual_weight,
			);
```

**File:** bridges/primitives/messages/src/target_chain.rs (L364-371)
```rust

```

**File:** bridges/modules/xcm-bridge-hub/src/dispatcher.rs (L88-129)
```rust
	fn dispatch(
		message: DispatchMessage<Self::DispatchPayload, Self::LaneId>,
	) -> MessageDispatchResult<Self::DispatchLevelResult> {
		let payload = match message.data.payload {
			Ok(payload) => payload,
			Err(e) => {
				tracing::error!(
					target: LOG_TARGET,
					error=?e,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - payload error"
				);
				return MessageDispatchResult {
					unspent_weight: Weight::zero(),
					dispatch_level_result: XcmBlobMessageDispatchResult::InvalidPayload,
				};
			},
		};
		let dispatch_level_result = match T::BlobDispatcher::dispatch_blob(payload) {
			Ok(_) => {
				tracing::debug!(
					target: LOG_TARGET,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - `DispatchBlob::dispatch_blob` was ok"
				);
				XcmBlobMessageDispatchResult::Dispatched
			},
			Err(e) => {
				tracing::error!(
					target: LOG_TARGET,
					error=?e,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - `DispatchBlob::dispatch_blob` failed"
				);
				XcmBlobMessageDispatchResult::NotDispatched(Some(e))
			},
		};
		MessageDispatchResult { unspent_weight: Weight::zero(), dispatch_level_result }
	}
```

**File:** bridges/modules/relayers/src/extension/mod.rs (L222-268)
```rust
		// We don't refund anything if the transaction has failed.
		if let Err(e) = result {
			tracing::trace!(
				target: LOG_TARGET,
				error=?e,
				id_provider=%Self::IDENTIFIER,
				?lane_id,
				?relayer,
				"Relayer has submitted invalid messages transaction",
			);
			return slash_relayer_if_delivery_result;
		}

		// check whether the call has succeeded
		let mut call_data = ExtensionCallData::default();
		if !C::check_call_result(&call_info, &mut call_data, &relayer) {
			return slash_relayer_if_delivery_result;
		}

		// regarding the tip - refund that happens here (at this side of the bridge) isn't the whole
		// relayer compensation. He'll receive some amount at the other side of the bridge. It shall
		// (in theory) cover the tip there. Otherwise, if we'll be compensating tip here, some
		// malicious relayer may use huge tips, effectively depleting account that pay rewards. The
		// cost of this attack is nothing. Hence we use zero as tip here.
		let tip = Zero::zero();

		// decrease post-dispatch weight/size using extra weight/size that we know now
		let post_info_len = len.saturating_sub(call_data.extra_size as usize);
		let mut post_info_weight = post_info
			.actual_weight
			.unwrap_or(info.total_weight())
			.saturating_sub(call_data.extra_weight);

		// let's also replace the weight of slashing relayer with the weight of rewarding relayer
		if call_info.is_receive_messages_proof_call() {
			post_info_weight = post_info_weight.saturating_sub(
				<R as RelayersConfig<C::BridgeRelayersPalletInstance>>::WeightInfo::extra_weight_of_successful_receive_messages_proof_call(),
			);
		}

		// compute the relayer refund
		let mut post_info = *post_info;
		post_info.actual_weight = Some(post_info_weight);
		let refund = Self::compute_refund(info, &post_info, post_info_len, tip);

		// we can finally reward relayer
		RelayerAccountAction::Reward(relayer, reward_account_params, refund.into())
```
