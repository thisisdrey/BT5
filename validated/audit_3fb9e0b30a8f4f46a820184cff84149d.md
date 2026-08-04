### Title
Unbound reward beneficiary in `submit_delivery_receipt` allows anyone to steal a relayer's Snowbridge delivery reward - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` (called from the public, unprivileged extrinsic `submit_delivery_receipt`) pays the `PendingOrder` fee to the transaction submitter (`relayer` = `ensure_signed(origin)`) whenever the verified `DeliveryReceipt.reward_address` field is the zero address. Because the delivery-receipt data (Ethereum event log + beacon/receipt Merkle proof) is public once the underlying Ethereum transaction is included, and because `submit_delivery_receipt` has no restriction tying the caller to the account that actually executed/paid for the Ethereum-side delivery, any unprivileged observer can copy that public proof and submit it first, redirecting the entire relayer fee to themselves instead of the party that did the real work of executing delivery on Ethereum.

### Finding Description
`submit_delivery_receipt` decodes and cryptographically verifies an Ethereum event log (`DeliveryReceipt`) via a beacon/receipt proof, then calls `process_delivery_receipt`: [1](#0-0) 

The key logic:
```
let reward_account = if receipt.reward_address == [0u8; 32] {
    relayer
} else {
    receipt.reward_address.into()
};
...
T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
``` [2](#0-1) 

`relayer` here is simply whoever calls `submit_delivery_receipt` (`ensure_signed(origin)`), with no restriction on who may call it (any of `PendingOrders`, benchmarking, and tests confirm any signed account may submit this extrinsic): [3](#0-2) 

This is structurally identical to the reported analog: the "reward-worthy work" (executing/relaying the message on Ethereum, at real gas cost) happens off-chain and is proven with public, replayable data (the Merkle/beacon proof of an already-included Ethereum transaction). Unlike the message-delivery-proof flow in `pallet-bridge-messages`, where the confirmation transaction proves the actual on-chain relayer identity from storage (`UnrewardedRelayer::relayer`) recorded during delivery — see `receive_messages_delivery_proof`, which reads `lane_data.relayers` (identities bound at delivery time) rather than trusting the caller of the confirmation extrinsic: [4](#0-3) 

the Snowbridge outbound-queue-v2 path does not bind any relayer identity into the verified proof data. It falls back to the raw extrinsic caller when `reward_address` is zero, so anyone who can see the finalized Ethereum block (a public activity, not requiring being a peer/validator/relayer) can construct the same beacon+receipt Merkle proof and race to call `submit_delivery_receipt`, taking the whole fee for themselves.

### Impact Explanation
This directly causes theft of relayer rewards (`order.fee`, funded on Ethereum by the message sender) intended for the account that performed the Ethereum-side delivery. It matches the "theft or unbacked... duplicate settlement or payout" category in the Impact Gate: the wrong beneficiary receives value that should have gone to the party that did the delivery work, and it requires no privileged role, no malicious validator/collator/relayer collusion — only observation of finalized Ethereum data, which any unprivileged Substrate account can act on.

### Likelihood Explanation
The precondition is simply that the relayer who executes delivery on Ethereum does not (or cannot always) set a non-zero `reward_address` in the Ethereum-side event. Any external actor monitoring the Ethereum Gateway contract's finalized events can construct the identical proof (data is fully public) and submit `submit_delivery_receipt` before the legitimate relayer does. This is a straightforward, unprivileged, always-available attack path whenever `reward_address == [0u8; 32]`, which appears to be the default/fallback path in the code (rather than a rare edge case), making exploitation plausible in normal operation.

### Recommendation
Bind the reward beneficiary to a value that cannot be raced/rewritten by a third party: either (a) require `reward_address` to always be non-zero at message-send time (reject/validate at the point the order fee is created, so there is no default fallback to `relayer`), or (b) record the identity of the account entitled to the reward in `PendingOrder` at enqueue time (`do_process_message`) instead of trusting whichever account happens to submit the delivery-receipt proof, mirroring how `pallet-bridge-messages` tracks `UnrewardedRelayer` identities on-chain rather than trusting the confirmation-transaction submitter.

### Proof of Concept
1. A user sends a message through the outbound queue with a fee; `do_process_message` creates a `PendingOrder{nonce, fee, ...}`.
2. RelayerA executes the corresponding transaction on the Ethereum Gateway contract, paying gas, but (as allowed by the flow) does not set a `reward_address` in the resulting event (or the default path sets it to zero).
3. Once the Ethereum transaction is included and finalized, its receipt and Merkle/beacon proof become public.
4. RelayerB (uninvolved in the Ethereum execution) constructs the identical proof from public data and calls `submit_delivery_receipt` first.
5. `process_delivery_receipt` sees `receipt.reward_address == [0u8;32]` and pays `order.fee` to RelayerB (`ensure_signed(origin)` of RelayerB's transaction), while RelayerA — who paid Ethereum gas to actually deliver the message — receives nothing.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/benchmarking.rs (L153-179)
```rust
	#[benchmark]
	fn submit_delivery_receipt() -> Result<(), BenchmarkError> {
		let caller: T::AccountId = whitelisted_caller();

		let message = make_submit_delivery_receipt_message();

		T::Helper::initialize_storage(message.finalized_header, message.block_roots_root);

		let receipt = DeliveryReceipt::try_from(&message.event.event_log).unwrap();

		let order = PendingOrder {
			nonce: receipt.nonce,
			fee: 0,
			block_number: frame_system::Pallet::<T>::current_block_number(),
		};
		<PendingOrders<T>>::insert(receipt.nonce, order);

		#[block]
		{
			assert_ok!(OutboundQueue::<T>::submit_delivery_receipt(
				RawOrigin::Signed(caller.clone()).into(),
				Box::new(message.event),
			));
		}

		Ok(())
	}
```

**File:** bridges/modules/messages/src/lib.rs (L386-411)
```rust
			// mark messages as delivered
			let mut lane = any_state_outbound_lane::<T, I>(lane_id)?;
			let last_delivered_nonce = lane_data.last_delivered_nonce();
			let confirmed_messages = lane
				.confirm_delivery(
					relayers_state.total_messages,
					last_delivered_nonce,
					&lane_data.relayers,
				)
				.map_err(Error::<T, I>::ReceptionConfirmation)?;

			if let Some(confirmed_messages) = confirmed_messages {
				// emit 'delivered' event
				let received_range = confirmed_messages.begin..=confirmed_messages.end;
				Self::deposit_event(Event::MessagesDelivered {
					lane_id: lane_id.into(),
					messages: confirmed_messages,
				});

				// if some new messages have been confirmed, reward relayers
				let actually_rewarded_relayers = T::DeliveryConfirmationPayments::pay_reward(
					lane_id,
					lane_data.relayers,
					&confirmation_relayer,
					&received_range,
				);
```
