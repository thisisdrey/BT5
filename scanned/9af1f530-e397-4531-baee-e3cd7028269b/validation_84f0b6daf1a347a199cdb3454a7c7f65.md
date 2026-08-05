### Title
Outbound bridge messages have no expiry or retry path if the delivery-receipt callback is never submitted, permanently locking `PendingOrder` state and relayer rewards - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`snowbridge-pallet-outbound-queue-v2` creates a `PendingOrder` entry for every outbound message committed to Ethereum, and that entry is only ever removed by the externally-triggered `submit_delivery_receipt` extrinsic (the on-chain "callback" analog to `ApiologyAuctionHouse::entropyCallback`). If that callback is never delivered — because no relayer submits the receipt, the beacon/Ethereum verifier is halted for an extended period, or the message is simply never executed on Ethereum — there is no timeout, retry, or reaping logic anywhere in the pallet to resolve the order. The `PendingOrder` (and the fee/reward tied to it) is stuck forever, exactly mirroring the reported bug class: an unprivileged external dependency's callback not firing halts protocol state with no fallback mechanism.

### Finding Description
`do_process_message` inserts a `PendingOrder { nonce, fee, block_number }` into `PendingOrders` for every outbound message and increments `Nonce`: [1](#0-0) 

The only code path that removes an entry from `PendingOrders` is `process_delivery_receipt`, invoked from the signed extrinsic `submit_delivery_receipt`, which requires an externally-supplied Ethereum event proof of message execution: [2](#0-1) [3](#0-2) 

The `PendingOrder` type carries a `block_number` field documenting when the order was created, but this field is never read anywhere to detect staleness or trigger any expiry/retry: [4](#0-3) 

There is no `on_initialize`/`on_finalize` sweep over `PendingOrders` to age out or resolve stale entries — the pallet's only hooks are message-commitment bookkeeping (`Messages`/`MessageLeaves` cleanup and merkle-root commit), not `PendingOrders` maintenance: [5](#0-4) 

The only other mutator of a `PendingOrder` is `add_tip`, which can *increase* the fee to incentivize relaying but cannot resolve, expire, or refund the order if delivery never happens: [6](#0-5) 

This is structurally identical to the reported vulnerability class: a critical state transition (order settlement / new-auction creation) is entirely gated on an externally-triggered callback (`entropyCallback` / `submit_delivery_receipt`) with **no timeout-based fallback**, so failure of the external actor to call back permanently stalls protocol state. Unlike the report's contract, there isn't even a documented "owner pause/unpause" workaround here — governance can halt the verifier (per `pr_11856.prdoc`) but that only blocks further processing; it does not resolve or expire existing stuck `PendingOrders`.

### Impact Explanation
Every stuck `PendingOrder` permanently occupies `PendingOrders` storage (unbounded growth over time as more messages fail to be relayed), and the fee/reward tied to it is never paid out to any relayer, nor is it returned/reallocated. Over time this is an accumulating, unrecoverable bridge-state lock: nonces for undelivered messages remain permanently "in flight" with no cleanup, retry, or reclaim path. This matches the "permanent... bridge-state lock" and "message queues/receipts/payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" impact categories, since here they can also permanently *fail to advance at all* with no recovery.

### Likelihood Explanation
Likelihood is low-to-moderate and passive rather than attacker-triggered: it requires an outbound message to simply never be relayed/delivered on the Ethereum side (relayer downtime, insufficient fee incentive, or an extended verifier halt as already documented in `pr_11856.prdoc`). No malicious relayer, validator, or governance action is needed — a message can get "orphaned" purely through relayer non-participation, the same "low likelihood, high impact, no adversary required" profile as the original report.

### Recommendation
Add an expiry/retry mechanism for `PendingOrder` keyed off the stored `block_number`: e.g., a periodic sweep (bounded per block) that, after a configurable timeout, either re-queues the message for re-delivery with an escalated fee, or marks/resolves the order through a permissionless "reap stale order" extrinsic that can refund/redirect the fee and remove the entry, so that unresponsive relayers cannot permanently strand bridge state.

### Proof of Concept
1. A message is sent from BridgeHub to Ethereum via `deliver`, causing `do_process_message` to create `PendingOrders[nonce] = { fee, block_number: N }`.
2. No relayer ever calls `submit_delivery_receipt` for `nonce` (simulate by simply not calling it, as in the existing test harness's `poc_m1`/`submit_delivery_receipt_succeeds_after_unhalt` tests which manually insert/remove `PendingOrder` entries): [7](#0-6) 
3. Advance the chain arbitrarily many blocks — `PendingOrders::<Test>::get(nonce)` remains `Some(order)` indefinitely; nothing in `on_initialize`/`on_finalize` or any other hook ever removes or expires it.
4. Confirm no code path other than `submit_delivery_receipt` clears `PendingOrders`, by inspecting all writers of the map (`do_process_message` insert, `process_delivery_receipt` remove, `add_tip` mutate) — none handle the "never delivered" case.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L273-286)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_initialize(_: BlockNumberFor<T>) -> Weight {
			// Remove storage from previous block
			Messages::<T>::kill();
			MessageLeaves::<T>::kill();
			// Reserve some weight for the `on_finalize` handler
			T::WeightInfo::on_initialize() + T::WeightInfo::commit()
		}

		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}
	}
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L14-24)
```rust
/// Pending order
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L392-416)
```rust
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}
```
