### Title
Relayer-controlled `dispatch_weight` parameter inflates `actual_weight` and drains the bridge rewards sovereign account in one transaction - (File: `bridges/modules/messages/src/lib.rs`)

### Summary
`pallet_bridge_messages::Call::receive_messages_proof` takes a caller-supplied `dispatch_weight: Weight` parameter that is used both to compute the extrinsic's declared (pre-dispatch) weight and as the starting value of `actual_weight`, which is only ever *decreased* by the real per-message weight consumed. `BridgeRelayersTransactionExtension::post_dispatch_details` then feeds this `actual_weight` into `compute_actual_fee`, and pays the resulting amount to the calling relayer out of a shared `RewardsAccountParams` sovereign account via `PayRewardFromAccount`. Any permissionless relayer can therefore submit a message-delivery transaction with a `dispatch_weight` value that is far larger than the real work performed, causing `actual_weight` to stay close to the inflated declared weight and draining the reward pool in a single call — the same "attacker sets an arbitrarily high gas parameter and gets paid the difference" primitive as the SponsorPaymaster report.

### Finding Description
In `receive_messages_proof`: [1](#0-0) 
`declared_weight` is computed directly from the caller-supplied `dispatch_weight` argument via `T::WeightInfo::receive_messages_proof_weight`, which adds it verbatim as `messages_dispatch_weight`: [2](#0-1) 

`actual_weight` starts at this inflated `declared_weight` and is only reduced by the real, content-derived `message_dispatch_weight`/`unspent_weight` of each message actually included in the proof: [3](#0-2) 
Note the per-message weight check only guards that `message_dispatch_weight ≤ dispatch_weight_left` — it never checks that the declared `dispatch_weight` is *reasonable* relative to the messages actually being delivered. A relayer can submit one small, cheap message together with a hugely inflated `dispatch_weight` parameter; the check at line 294 trivially passes because `dispatch_weight_left` starts enormous, and the final `actual_weight` remains close to the attacker-chosen value because only the tiny real message weight is subtracted.

This `actual_weight` becomes the extrinsic's `PostDispatchInfo.actual_weight`: [4](#0-3) 

`BridgeRelayersTransactionExtension::analyze_call_result`/`compute_refund` then uses this inflated `post_info.actual_weight` to compute a "refund"/reward that is paid to the relayer: [5](#0-4) 
and `post_dispatch_details` registers this reward for immediate payout from the shared rewards account: [6](#0-5) 

The payout itself is a `fungible::transfer` from a fixed sovereign "rewards account" (funded once by the bridged parachain, per the bridge docs) directly to the calling relayer: [7](#0-6) [8](#0-7) 

No unprivileged actor is required to be a validator, collator, governance actor, or hold any special role — `receive_messages_proof` is signed and callable by anyone (`ensure_signed(origin)?`), exactly mirroring the "malicious bundler"/unprivileged caller primitive in the source report, only here the "gas parameter" is `dispatch_weight` and the paymaster is the bridge `RewardsAccountParams` sovereign account.

### Impact Explanation
The bridge rewards sovereign account is a fixed pool funded by the connected parachain to compensate relayers for genuine work. Because `actual_weight` is only bound below by real per-message costs and above by the attacker-chosen `dispatch_weight` (subject only to the block/extrinsic max-weight limit, not to anything tied to the messages actually delivered), a malicious relayer can extract a reward corresponding to close to the maximum extrinsic weight in a single transaction while doing negligible real work (e.g., delivering/re-processing one trivial message). Repeated submissions can drain the account entirely, denying honest relayers their compensation and potentially stalling message delivery once the reward pool is emptied — a direct fund-theft/duplicate-settlement analog with chain/bridge processing impact.

### Likelihood Explanation
The attack requires only a signed account able to submit `receive_messages_proof` with a valid (even minimal) message proof — no special privilege, validator/collator role, or governance action is needed. The bounding mechanism (per-message weight check against `dispatch_weight_left`) does not constrain the relationship between the declared `dispatch_weight` and the actual weight of messages delivered, so the exploit is mechanically straightforward given a valid inbound lane and a cheap message to include as the "vehicle" for an inflated `dispatch_weight`.

### Recommendation
Bound `actual_weight` reported from `receive_messages_proof` (and any weight fed into `BridgeRelayersTransactionExtension::compute_refund`) to the sum of the *real* per-message dispatch weights actually computed by `T::MessageDispatch::dispatch_weight`, rather than allowing it to float near the caller-supplied `dispatch_weight` declared weight. Alternatively/additionally, validate that the declared `dispatch_weight` is proportionate to the number/size of messages in the proof before accepting the transaction, and cap the maximum reward payable per transaction from the shared rewards account independent of the reported post-dispatch weight.

### Proof of Concept
1. Prepare a valid inbound lane and a minimal message-delivery proof containing exactly one very small message.
2. Craft the `receive_messages_proof(origin, proof, messages_count = 1, dispatch_weight = <near block-max weight>)` extrinsic, setting `dispatch_weight` far larger than the real dispatch cost of the single message.
3. Because `T::WeightInfo::receive_messages_proof_weight` adds `dispatch_weight` directly as `messages_dispatch_weight`, the extrinsic's declared weight and starting `actual_weight` are both inflated to the caller-chosen value [2](#0-1) .
4. During dispatch, only the tiny real per-message weight is subtracted from `actual_weight` [9](#0-8) , so `PostDispatchInfo.actual_weight` returned remains close to the inflated value.
5. `BridgeRelayersTransactionExtension::post_dispatch_details` computes a large refund/reward from this inflated weight and calls `register_relayer_reward`, crediting the attacker's relayer account against the shared `RewardsAccountParams` account [10](#0-9) .
6. The attacker calls `claim_rewards`/the reward is settled via `PayRewardFromAccount::pay_reward`, transferring balance out of the shared rewards sovereign account into the attacker's own account [11](#0-10) .
7. Repeating this across multiple transactions (bounded only by max extrinsic weight per call) drains the rewards account.

### Citations

**File:** bridges/modules/messages/src/lib.rs (L241-246)
```rust
			let declared_weight = T::WeightInfo::receive_messages_proof_weight(
				&*proof,
				messages_count,
				dispatch_weight,
			);
			let mut actual_weight = declared_weight;
```

**File:** bridges/modules/messages/src/lib.rs (L290-332)
```rust
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

**File:** bridges/modules/messages/src/lib.rs (L353-353)
```rust
			Ok(PostDispatchInfo { actual_weight: Some(actual_weight), pays_fee: Pays::Yes })
```

**File:** bridges/modules/messages/src/weights_ext.rs (L301-328)
```rust
	fn receive_messages_proof_weight(
		proof: &impl Size,
		messages_count: u32,
		dispatch_weight: Weight,
	) -> Weight {
		// basic components of extrinsic weight
		let base_weight = Self::receive_n_messages_proof(messages_count);
		let transaction_overhead_from_runtime =
			Self::receive_messages_proof_overhead_from_runtime();
		let outbound_state_delivery_weight =
			Self::receive_messages_proof_outbound_lane_state_overhead();
		let messages_dispatch_weight = dispatch_weight;

		// proof size overhead weight
		let expected_proof_size = EXPECTED_DEFAULT_MESSAGE_LENGTH
			.saturating_mul(messages_count.saturating_sub(1))
			.saturating_add(Self::expected_extra_storage_proof_size());
		let actual_proof_size = proof.size();
		let proof_size_overhead = Self::storage_proof_size_overhead(
			actual_proof_size.saturating_sub(expected_proof_size),
		);

		base_weight
			.saturating_add(transaction_overhead_from_runtime)
			.saturating_add(outbound_state_delivery_weight)
			.saturating_add(messages_dispatch_weight)
			.saturating_add(proof_size_overhead)
	}
```

**File:** bridges/modules/relayers/src/extension/mod.rs (L248-280)
```rust
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
	}

	/// Compute refund for the successful relayer transaction
	fn compute_refund(
		info: &DispatchInfo,
		post_info: &PostDispatchInfo,
		len: usize,
		tip: <<R as TransactionPaymentConfig>::OnChargeTransaction as OnChargeTransaction<R>>::Balance,
	) -> <<R as TransactionPaymentConfig>::OnChargeTransaction as OnChargeTransaction<R>>::Balance
	{
		TransactionPaymentPallet::<R>::compute_actual_fee(len as _, info, post_info, tip)
	}
```

**File:** bridges/modules/relayers/src/extension/mod.rs (L382-419)
```rust
	fn post_dispatch_details(
		pre: Self::Pre,
		info: &DispatchInfoOf<R::RuntimeCall>,
		post_info: &PostDispatchInfoOf<R::RuntimeCall>,
		len: usize,
		result: &DispatchResult,
	) -> Result<Weight, TransactionValidityError> {
		let lane_id = pre.as_ref().map(|p| p.call_info.messages_call_info().lane_id());
		let call_result = Self::analyze_call_result(pre, info, post_info, len, result);

		match call_result {
			RelayerAccountAction::None => (),
			RelayerAccountAction::Reward(relayer, reward_account, reward) => {
				RelayersPallet::<R, C::BridgeRelayersPalletInstance>::register_relayer_reward(
					reward_account.into(),
					&relayer,
					reward,
				);

				tracing::trace!(
					target: LOG_TARGET,
					id_provider=%Self::IDENTIFIER,
					?lane_id,
					?relayer,
					?reward,
					"Has registered reward"
				);
			},
			RelayerAccountAction::Slash(relayer, slash_account) => {
				RelayersPallet::<R, C::BridgeRelayersPalletInstance>::slash_and_deregister(
					&relayer,
					ExplicitOrAccountParams::Params(slash_account),
				)
			},
		}

		Ok(Weight::zero())
	}
```

**File:** bridges/primitives/relayers/src/lib.rs (L163-189)
```rust
impl<T, Relayer, LaneId, RewardBalance>
	PaymentProcedure<Relayer, RewardsAccountParams<LaneId>, RewardBalance>
	for PayRewardFromAccount<T, Relayer, LaneId, RewardBalance>
where
	T: frame_support::traits::fungible::Mutate<Relayer>,
	T::Balance: From<RewardBalance>,
	Relayer: Clone + Debug + Decode + Encode + Eq + TypeInfo,
	LaneId: Decode + Encode,
{
	type Error = sp_runtime::DispatchError;
	type Beneficiary = Relayer;

	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
}
```

**File:** bridges/docs/polkadot-kusama-bridge-overview.md (L90-98)
```markdown
### Who is Rewarding Relayers

Obviously, there should be someone who is paying relayer rewards. We want bridge transactions to have a cost, so we
can't use fees for rewards. Instead, the parachains using the bridge, use sovereign accounts on both sides of the bridge
to cover relayer rewards.

Bridged Parachains will have sovereign accounts at bridge hubs. For example, the Kusama Asset Hub will
have an account at the Polkadot Bridge Hub. The Polkadot Asset Hub will have an account at the Kusama
Bridge Hub. The sovereign accounts are used as a source of funds when the relayer is calling the
```
