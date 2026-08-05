This confirms `RewardLedger::register_reward` at `bridges/primitives/relayers/src/lib.rs:217-220` registers a credit that is later claimable via `PaymentProcedure::pay_reward`, which triggers a real `fungible::Mutate::transfer` (an actual balance movement) as seen in `PayRewardFromAccount::pay_reward` at lines 175-188. The claim is fully supported by the code as written.

Audit Report

## Title
`process_delivery_receipt` credits relayer reward to an unchecked, event-supplied `reward_address` and ignores `receipt.success`, decoupling payout from actual delivery outcome - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`submit_delivery_receipt` (a public, unprivileged, signed extrinsic) verifies only that a Merkle/beacon proof for an Ethereum log is valid and that the log originates from the configured `T::GatewayAddress`; it then calls `process_delivery_receipt`, which decodes `DeliveryReceipt { gateway, nonce, topic, success, reward_address }` but never inspects `receipt.success` before unconditionally calling `T::RewardPayment::register_reward` for `order.fee`, and it derives the payout beneficiary directly from the attacker/relayer-influenced `receipt.reward_address` field. This decouples reward settlement from actual successful execution on Ethereum and allows the beneficiary of the payout to diverge from the account that performed the on-chain relaying work.

## Finding Description
The extrinsic `submit_delivery_receipt` is reachable by any signed account with no additional permission gate [1](#0-0) . It verifies the Merkle/beacon proof and decodes the log into a `DeliveryReceipt` that includes an unchecked `success: bool` and `reward_address: [u8; 32]` field defined in the ABI event schema [2](#0-1) .

`process_delivery_receipt` then only checks that `receipt.gateway` matches `T::GatewayAddress` and that a `PendingOrder` exists for `receipt.nonce`; the `reward_account` is computed directly from `receipt.reward_address` (falling back to the submitting `relayer` only if it is all-zero), and `order.fee` is paid unconditionally via `T::RewardPayment::register_reward` whenever `fee > 0` — `receipt.success` is never read anywhere in the pallet [3](#0-2) . A repository-wide search confirms `success` is decoded into the struct but has zero other references within the `outbound-queue-v2` pallet, i.e., it is dead data with respect to the payout decision.

`register_reward` is not a no-op bookkeeping call: it implements `RewardLedger::register_reward`, which records a claimable balance later redeemed through `PaymentProcedure::pay_reward`, whose default `PayRewardFromAccount` implementation performs a real `fungible::Mutate::transfer` of funds from the bridge's rewards sovereign account to the beneficiary [4](#0-3) . Thus a successful call to `process_delivery_receipt` results in genuine value transfer to whatever 32-byte account is present in the untrusted `reward_address` field of the externally supplied event log.

The existing guards — Merkle/beacon proof verification and `gateway`-address equality — only establish that the log is authentic and came from the correct contract; they do nothing to (a) bind the payout to `receipt.success == true`, or (b) bind `reward_address` to any pallet-recorded, trusted value set at message-queuing time (unlike `PendingOrder`, which is created and controlled entirely by the pallet at `send_message_impl`/`do_process_message` time) [5](#0-4) .

## Impact Explanation
This breaks the required invariant that bridge payout state must only advance after execution and settlement succeed atomically: a relayer is paid full `order.fee` regardless of whether the dispatched Ethereum-side commands actually executed successfully (`success=false` case is fully unguarded). Additionally, because `reward_address` is taken verbatim from the Ethereum-emitted log rather than from pallet-controlled state, the beneficiary of the payout is not bound to the entity that performed the relaying work on the Substrate side (the `relayer` who signed `submit_delivery_receipt`), allowing value to be steered to an unintended account whenever an attacker can influence what `reward_address` ends up encoded in the log for a given nonce. This matches the "theft or unbacked mint/unlock" and "duplicate settlement or payout to wrong beneficiary" impact categories.

## Likelihood Explanation
`submit_delivery_receipt` requires only `ensure_signed(origin)` and a valid Merkle/beacon proof of a genuine Gateway-emitted log [6](#0-5) . Every dispatched message on Ethereum, successful or not, produces exactly such a log, so triggering payment for a failed delivery, or for a reward address unrelated to the actual relayer, requires no special permission and is repeatable per nonce.

## Recommendation
- Add `ensure!(receipt.success, Error::<T>::DeliveryFailed)` (or explicitly branch to skip/reduce payment and emit a distinct `MessageDeliveryFailed` event) before calling `register_reward`.
- Consider sourcing the reward beneficiary from pallet-side state recorded at message-queuing time (e.g., stored in `PendingOrder`) instead of trusting the verbatim `reward_address` field decoded from the Ethereum log, removing the ability for an externally-controlled event field to redirect payouts.

## Proof of Concept
1. `do_process_message` queues a message with `fee > 0`, inserting `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, .. })` [7](#0-6) .
2. The Gateway contract on Ethereum dispatches the message but the inner command execution fails, so it emits `InboundMessageDispatched(nonce, topic, success=false, reward_address=<attacker-chosen 32 bytes>)`.
3. Any signed account submits `submit_delivery_receipt` with a valid proof of this log.
4. `process_delivery_receipt` checks only `gateway` and `PendingOrders` existence, ignores `success == false`, and calls `register_reward(&reward_account, ..., order.fee)` for the attacker-chosen `reward_account`, exactly as demonstrated by the existing test that calls `process_delivery_receipt` directly with a caller-constructed `DeliveryReceipt{ success: true, reward_address, .. }` and observes `RewardRegistered` [8](#0-7) ; the same call flow succeeds identically with `success: false`.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
```

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-426)
```rust
		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
