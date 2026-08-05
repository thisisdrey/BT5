## Analysis

The Aleo `split` bug's core broken invariant is: **a public, permissionless entrypoint whose consensus-level cost is decoupled from the actual resource/verification work it forces the network to perform, so an attacker can repeatedly submit it near a fixed, cheap cost even as congestion rises, crowding out legitimate work.**

The closest verifiable local analog is in the bridge messages pallet's delivery-proof dispatchable, which explicitly implements a refund path that lets a relayer replay a proof containing already-delivered (stale) message nonces while only being charged the base/verification cost, not the full declared cost, and whose associated priority boost is keyed purely to the *declared* message count rather than to actually-dispatched work. [1](#0-0) [2](#0-1) 

The code comment itself acknowledges the spam pattern but asserts it is bounded because "relayer pays base delivery transaction cost anyway": [3](#0-2) 

However, `weights_ext.rs` shows the base cost's PoV `proof_size` component is intentionally invariant to `messages_count` (it is benchmarked/asserted to *not* scale with the number of messages in the proof): [4](#0-3) 

And the transaction-priority boost given to bridge relayer submissions is computed strictly from the *declared* number of items (`n_items`), not from the number of messages actually dispatched/paid for: [5](#0-4) 

### Title
Underpriced replay of stale-message delivery proofs lets a relayer cheaply occupy bridge lanes and win priority — (`bridges/modules/messages/src/lib.rs`)

### Summary
`Pallet::receive_messages_proof` refunds the relayer for every message that is not actually dispatched (including messages rejected because their nonce was already delivered), subtracting `message_dispatch_weight` from the charged `actual_weight`. Only the base/verification portion of the declared weight remains chargeable, and that base weight component is explicitly designed to be independent of `messages_count`. Combined with `pallet-bridge-relayers`' priority boost, which scores transactions purely on the *declared* message count, an attacker-relayer can submit maximal-size proofs referencing already-delivered nonces (or a mix designed to hit `TooManyUnrewardedRelayers`/`TooManyUnconfirmedMessages`) repeatedly, paying close to the fixed base cost each time while claiming the highest transaction priority a lane submission can earn.

### Finding Description
`receive_messages_proof` computes `declared_weight` from the call arguments before doing any work, then loops over decoded messages and, for `ReceptionResult::InvalidNonce`, `TooManyUnrewardedRelayers`, and `TooManyUnconfirmedMessages` results, treats the entire `message_dispatch_weight` as unspent and subtracts it from `actual_weight`: [6](#0-5) 

The only remaining charge is `declared_weight` minus the per-message dispatch component, i.e. essentially `receive_n_messages_proof(messages_count)` base weight plus proof-size overhead. But `weights_ext.rs`'s own correctness assertions establish that the storage/PoV portion of this cost does *not* grow with `messages_count` beyond the (small) benchmark baseline — it is a near-flat "connection/verification" charge, similar in spirit to Aleo's `split` charging a flat 10-credit fee regardless of the surrounding congestion or actual chain-state work induced.

At the same time, `pallet-bridge-relayers`'s `compute_priority_boost` grants extra transaction-pool priority proportional to `n_items` (the declared message count in the call, not the number of messages that were ultimately dispatched or paid for): [5](#0-4) 

Because the priority boost is derived from the same declared arguments used to compute the (largely refunded) weight, a relayer can craft a proof naming the maximum allowed number of stale/duplicate nonces to earn maximum priority boost while paying close to the flat base fee once the dispatch-weight portion is refunded post-dispatch. This lets the transaction win block inclusion priority over legitimate, fully-payable message deliveries — an underpriced-work pattern directly analogous to the `split` transaction's fixed fee letting it crowd out organically-priced transactions during congestion.

### Impact Explanation
This falls under the in-scope category of "public underpriced work that degrades block production or stalls bridge processing." A relayer can repeatedly occupy a bridge lane's inbound-lane storage slot and the block's transaction slot at near-fixed cost, while simultaneously outranking honest relayers' legitimately fee-scaled deliveries via the count-based priority boost — stalling forward progress of the lane (only one message per block can make progress if higher-nonce messages get crowded out, as the module's own comment on `PriorityBoostPerItem` notes) and degrading Bridge Hub throughput during congestion.

### Likelihood Explanation
The attack requires only a signed account and knowledge of already-delivered nonces (which are public on-chain state via `InboundLanes`), no privileged role, validator, or relayer collusion is needed — it is a pure public-entrypoint interaction, matching the "unprivileged attacker" requirement of the impact gate.

### Recommendation
- Do not refund the dispatch-weight component for messages rejected due to `InvalidNonce`/`TooManyUnrewardedRelayers`/`TooManyUnconfirmedMessages`; charge relayers the full declared weight for proofs containing stale/duplicate nonces so cost tracks the number of items claimed.
- Compute the priority boost from the number of *actually accepted* messages (post-dispatch), not the declared `messages_count`/`n_items` argument, so priority cannot be inflated by supplying stale nonces.

### Proof of Concept
1. Observe the current `InboundLanes` state for a lane to find the latest delivered nonce `N`.
2. Craft a `receive_messages_proof` call naming `messages_count` at `MAX_UNCONFIRMED_MESSAGES_IN_CONFIRMATION_TX` worth of already-delivered nonces (`≤ N`), which will resolve to `ReceptionResult::InvalidNonce` for each message in the loop at [7](#0-6) .
3. Submit repeatedly; `actual_weight` returned in `PostDispatchInfo` is `declared_weight` minus the full per-message dispatch weight for every stale message, so the relayer is billed close to base overhead each time, while `pallet-bridge-relayers`'s `compute_priority_boost` (keyed to the declared `n_items`) grants it the maximum available priority boost, letting it win block space over legitimately-priced deliveries.

### Citations

**File:** bridges/modules/messages/src/lib.rs (L212-220)
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
```

**File:** bridges/modules/messages/src/lib.rs (L290-331)
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
```

**File:** bridges/modules/messages/src/weights_ext.rs (L174-194)
```rust
/// Panics if `proof_size` of message delivery call depends on the messages count.
///
/// In practice, it will depend on the messages count, because most probably every
/// message will read something from db during dispatch. But this must be accounted
/// by the `dispatch_weight`.
fn messages_count_does_not_affect_proof_size<W: WeightInfoExt>() {
	let messages_proof_size = PreComputedSize(8 * 1024);
	let dispatch_weight = Weight::zero();
	let weight_of_one_incoming_message =
		W::receive_messages_proof_weight(&messages_proof_size, 1, dispatch_weight);
	let weight_of_two_incoming_messages =
		W::receive_messages_proof_weight(&messages_proof_size, 2, dispatch_weight);

	ensure_weight_components_are_not_zero(weight_of_one_incoming_message);
	ensure_weight_components_are_not_zero(weight_of_two_incoming_messages);
	ensure_proof_size_is_the_same(
		weight_of_one_incoming_message,
		weight_of_two_incoming_messages,
		"Number of same-lane incoming messages does not affect values that we read from our storage",
	);
}
```

**File:** bridges/modules/relayers/src/extension/priority.rs (L36-45)
```rust
/// Compute priority boost for transaction that brings given number of bridge
/// items (messages, headers, ...), when every additional item adds `PriorityBoostPerItem`
/// to transaction priority.
pub fn compute_priority_boost<PriorityBoostPerItem>(n_items: ItemCount) -> TransactionPriority
where
	PriorityBoostPerItem: Get<TransactionPriority>,
{
	// we don't want any boost for transaction with single (additional) item => minus one
	PriorityBoostPerItem::get().saturating_mul(n_items.saturating_sub(1))
}
```
