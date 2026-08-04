Confirmed: `DeliveryReceipt` carries a `success: bool` field decoded straight from the Ethereum `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log [1](#0-0) , and `Pallet::process_delivery_receipt` in `EthereumOutboundQueueV2` never inspects `receipt.success` before paying out the relayer's fee and clearing the order — it only checks the gateway address and that a `PendingOrder` exists for the nonce.

### Title
Unconditional relayer reward payout ignoring on-chain delivery `success` flag in `submit_delivery_receipt` — ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core broken invariant is: *a reward-distribution routine pays out based on the fact of a submission rather than on whether that submission represents genuine, successful, useful work*, letting an actor collect a payout without having delivered real value. `pallet_bridge_relayers`/`pallet_bridge_messages` avoid this by rewarding only messages that were actually and newly delivered [2](#0-1) . The Snowbridge V2 outbound queue does not apply the same discipline: `process_delivery_receipt` pays the full `order.fee` for any nonce with a matching `PendingOrder`, without checking whether the Ethereum-side execution of the message actually succeeded.

### Finding Description
`DeliveryReceipt::try_from` decodes the `success` field straight out of the Ethereum `InboundMessageDispatched` event log [3](#0-2) . That field is meant to indicate whether the corresponding command actually executed successfully on the Gateway contract on Ethereum.

`Pallet::process_delivery_receipt`, however, never reads or gates on `receipt.success`: [4](#0-3) 

It only:
1. Checks `receipt.gateway == GatewayAddress` (binding to the correct contract).
2. Resolves `reward_account` from `receipt.reward_address` (or falls back to the calling relayer).
3. Looks up the `PendingOrder` by `receipt.nonce`.
4. Pays `order.fee` unconditionally if `order.fee > 0`.
5. Removes the order — this happens regardless of the `success` flag.

Because verification of the beacon/receipts proof (`T::Verifier::verify`) only proves that this particular log was included and finalized on Ethereum — not that the message's on-chain effect was successful — a relayer can submit any legitimately-emitted `InboundMessageDispatched` log where `success == false` (i.e. the message reverted/failed on Ethereum for any reason: insufficient gas provided, a reverting XCM execution, or any other failure condition that the Gateway still logs) and still collect the full relaying fee that was meant to compensate *successful* delivery.

This is a direct structural analog to the report's core flaw: the payout condition ("a valid receipt for this nonce exists") is weaker than the actual definition of "useful work done" ("the message was successfully executed"), so the reward pool can be drained by submissions that do not represent genuine successful relaying — exactly the "dilution of rewards for legitimate work" and "reward without doing real work" pattern from the report, but manifesting as unconditional payout on any receipt rather than on lateness.

### Impact Explanation
Every `PendingOrder` fee (funded from `Message.fee`, i.e. real Ether value escrowed for the specific message) can be paid out to whichever account is named in `reward_address` even when the underlying Ethereum-side command failed. This is a direct value-conservation violation in the "bridge rewards ... settle exactly once to the rightful beneficiary" sense: the fee is meant to reward *successful* delivery work, and paying it out for failed deliveries is equivalent to an unbacked reward disbursement — draining the fee escrow without the corresponding service being rendered. Any Ethereum account can trigger a reverting/failing message dispatch (deliberately under-providing gas, or crafting a command doomed to fail) and then relay the corresponding failure receipt to still collect `order.fee`, at no cost to correctness checks in this pallet.

### Likelihood Explanation
High. `submit_delivery_receipt` is a fully public, unprivileged extrinsic — `ensure_signed(origin)` is the only signer check, and no `RewardKind`/success gating exists [5](#0-4) . The `success` value is entirely attacker-influenceable on the Ethereum execution side (revert conditions, gas griefing) yet is silently discarded once decoded on the Polkadot side. No malicious relayer/validator/governance assumption is needed beyond a normal unprivileged actor being able to submit an Ethereum transaction and later relay its (valid) receipt log.

### Recommendation
Gate the reward payment in `process_delivery_receipt` on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `false` receipts, still remove/settle the `PendingOrder` (to release the escrow and stop further claims), but route the fee to a well-defined failure path (e.g., refund to sender, or a configurable "failed delivery" burn / retry mechanism) rather than paying the relayer a delivery reward for a failed message.

### Proof of Concept
1. A `Message` is enqueued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, ... }` [6](#0-5) .
2. The relayer (or anyone) delivers the corresponding message to the Ethereum Gateway but arranges for it to fail execution (e.g., insufficient gas relative to `command.gas`, or a command with an inherently reverting inner XCM). The Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a receipts/beacon proof for this (real, finalized) log and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds (the log is genuinely included/finalized); `DeliveryReceipt::try_from` decodes `success = false` but this is never checked.
5. `process_delivery_receipt` reads `PendingOrders[nonce]`, sees `fee = F > 0`, and calls `T::RewardPayment::register_reward(&reward_account, ..., F)`, then removes the order — reward is paid despite `success == false`.

Note: I could not fully trace how `AddTip`/tipping combined with this path interacts with governance-configured `RewardPayment` implementations at runtime level (e.g., in `bridge-hub-westend`), so the exact downstream token accounting (e.g., how `pallet_bridge_relayers::claim_rewards_to` eventually debits the sovereign/escrow account) was not re-verified beyond the citations shown; a Devin session with full build/test access would be needed to add a regression test asserting no reward is registered when `receipt.success == false`.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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
