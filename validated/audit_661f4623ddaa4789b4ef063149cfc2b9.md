### Title
Unauthenticated `relayer_id_at_bridged_chain` in `pallet_bridge_messages::receive_messages_proof` lets anyone front-run a relayer and steal delivery rewards - (File: bridges/modules/messages/src/lib.rs)

### Summary
`Pallet::receive_messages_proof` accepts a caller-supplied `relayer_id_at_bridged_chain: AccountIdOf<BridgedChainOf<T, I>>` parameter that is stored per-nonce in `InboundLaneData.relayers` and later used — at delivery-confirmation time on the source chain — to determine who is credited with the message-delivery reward via `bp_messages::calc_relayers_rewards`. The extrinsic only requires `ensure_signed(origin)` and never checks that the signer/origin has any relationship to the `relayer_id_at_bridged_chain` value it supplies. [1](#0-0) 

### Finding Description
The message-delivery proof itself (`FromBridgedChainMessagesProof`) is public, verifiable data derived from the finalized state of the bridged (source) chain — anyone who observes the source chain can reconstruct and submit it once it is available, exactly like the RNG relay auction result in the referenced PoolTogether report was public data anyone could relay once produced. `receive_messages_proof` is a fully public dispatchable: it does no access control beyond "is signed", and it lets the caller freely choose the `relayer_id_at_bridged_chain` value that gets attributed as the deliverer of the messages for reward-accounting purposes on the other side of the bridge. [1](#0-0) 

This value is recorded as the `relayer` field of `UnrewardedRelayer` inside `InboundLaneData.relayers`, and is later consumed by `bp_messages::calc_relayers_rewards`, which is invoked from `DeliveryConfirmationPaymentsAdapter::pay_reward`/`register_relayers_rewards` to register reward balances keyed by that very account. [2](#0-1) 

Because there is no binding between `msg.sender` (the actual transaction submitter/relayer at the target chain) and `relayer_id_at_bridged_chain` (the account that will be paid on the source chain), an unrelated party can watch the source chain for a not-yet-relayed message batch, build the exact same delivery proof (it's derived only from public source-chain state) and submit `receive_messages_proof` first with an arbitrary `relayer_id_at_bridged_chain` — their own account on the source chain — before the honest relayer who actually did the off-chain retrieval work gets a chance to submit it. The honest relayer's proof submission then fails with `Error::<T,I>::InvalidMessagesProof`/nonce-already-delivered style rejection (since the nonces are already marked delivered), and the messages are permanently recorded as delivered by the attacker's chosen account, which later collects the reward through `receive_messages_delivery_proof` → `calc_relayers_rewards` on the source chain.

This mirrors the structural flaw in the PoolTogether `rngComplete` finding precisely: a public, unauthenticated entry point consumes previously/publicly generated data and lets the caller freely name the reward beneficiary, with no check binding the beneficiary to the entity that actually performed the underlying work.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for bridge relayer rewards. An attacker with no special privileges (no need to be a validator/collator/governance actor) can systematically capture message-delivery rewards intended for the honest relayers who actually maintain a given bridge lane, using nothing but public chain data and a signed extrinsic — undercutting relayer incentives and potentially disincentivizing honest relaying of the bridge (affecting bridge liveness), which is in-scope under "public underpriced work that degrades ... stalls bridge processing" and "duplicate settlement or payout ... to the rightful beneficiary."

### Likelihood Explanation
Likelihood is high for an economically motivated but otherwise unprivileged attacker: they need only monitor the source chain for queued/undelivered message ranges, replicate the storage proof construction (public algorithm, no secrets involved, same code the legitimate relay software uses) and submit it with their own `relayer_id_at_bridged_chain`. No governance, validator, or leaked-key assumption is required, satisfying the "front-run-only" caveat's opposite: this doesn't need to front-run a *specific pending transaction*, it only needs to be first to submit an available, publicly-reconstructible proof — a race any relayer client can already win by adjusting fee/priority, similar to how any auction bot could call `rngComplete` first.

### Recommendation
Bind the reward-eligible account to the actual transaction signer, or otherwise authenticate `relayer_id_at_bridged_chain`:
- Either remove the free-form `relayer_id_at_bridged_chain` argument and derive the credited account from `ensure_signed(origin)` plus a chain-mapping mechanism, or
- Require a signature/proof linking the origin account at the target chain to the claimed `relayer_id_at_bridged_chain` account at the source chain (e.g., a registered relayer mapping in `pallet-bridge-relayers`), so only a party that legitimately controls that source-chain account can claim the reward.

### Proof of Concept
1. Source chain accumulates outbound messages `[N..M]` in lane `L` (any user's XCM/messages, publicly visible via chain state/storage proof).
2. Honest relayer `R_source`/`R_target` prepares to submit `receive_messages_proof { relayer_id_at_bridged_chain: R_source, proof, messages_count, dispatch_weight }` to the target chain, as shown in the existing relayer helper `DirectReceiveMessagesProofCallBuilder::build_receive_messages_proof_call`. [3](#0-2) 
3. Attacker `A` independently reconstructs the same `FromBridgedChainMessagesProof` from public source-chain state (no special access needed) and submits `receive_messages_proof { relayer_id_at_bridged_chain: A, proof, messages_count, dispatch_weight }` from their own signed account, winning the race to be included first.
4. `Pallet::receive_messages_proof` executes with only `ensure_signed(origin)?` checked — no verification that `A` is `R_source` or otherwise entitled — and stores `UnrewardedRelayer { relayer: A, ... }` in `InboundLaneData.relayers` for nonces `[N..M]`. [1](#0-0) 
5. `R_source`'s later submission of the identical proof fails (nonces already delivered).
6. When `receive_messages_delivery_proof` is later submitted on the source chain, `calc_relayers_rewards`/`register_relayers_rewards` credits the delivery reward to `A`, not `R_source`, permanently diverting the payout. [2](#0-1)

### Citations

**File:** bridges/modules/messages/src/lib.rs (L212-222)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::receive_messages_proof_weight(&**proof, *messages_count, *dispatch_weight))]
		pub fn receive_messages_proof(
			origin: OriginFor<T>,
			relayer_id_at_bridged_chain: AccountIdOf<BridgedChainOf<T, I>>,
			proof: Box<FromBridgedChainMessagesProof<HashOf<BridgedChainOf<T, I>>, T::LaneId>>,
			messages_count: u32,
			dispatch_weight: Weight,
		) -> DispatchResultWithPostInfo {
			Self::ensure_not_halted().map_err(Error::<T, I>::BridgeModule)?;
			let relayer_id_at_this_chain = ensure_signed(origin)?;
```

**File:** bridges/modules/relayers/src/payment_adapter.rs (L52-74)
```rust
	fn pay_reward(
		lane_id: LaneIdOf<T, MI>,
		messages_relayers: VecDeque<bp_messages::UnrewardedRelayer<T::AccountId>>,
		confirmation_relayer: &T::AccountId,
		received_range: &RangeInclusive<bp_messages::MessageNonce>,
	) -> MessageNonce {
		let relayers_rewards =
			bp_messages::calc_relayers_rewards::<T::AccountId>(messages_relayers, received_range);
		let rewarded_relayers = relayers_rewards.len();

		register_relayers_rewards::<T, RI, MI>(
			confirmation_relayer,
			relayers_rewards,
			RewardsAccountParams::new(
				lane_id,
				T::BridgedChain::ID,
				RewardsAccountOwner::BridgedChain,
			),
			DeliveryReward::get(),
		);

		rewarded_relayers as _
	}
```

**File:** bridges/relays/lib-substrate-relay/src/messages/mod.rs (L403-416)
```rust
	fn build_receive_messages_proof_call(
		relayer_id_at_source: AccountIdOf<P::SourceChain>,
		proof: SubstrateMessagesProof<P::SourceChain, P::LaneId>,
		messages_count: u32,
		dispatch_weight: Weight,
		trace_call: bool,
	) -> CallOf<P::TargetChain> {
		let call: CallOf<P::TargetChain> = BridgeMessagesCall::<R, I>::receive_messages_proof {
			relayer_id_at_bridged_chain: relayer_id_at_source,
			proof: proof.1.into(),
			messages_count,
			dispatch_weight,
		}
		.into();
```
