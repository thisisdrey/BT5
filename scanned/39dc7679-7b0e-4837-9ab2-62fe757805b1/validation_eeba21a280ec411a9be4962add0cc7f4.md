### Title
Delivery fee/tip permanently locked in `PendingOrders` with no reclaim path if a delivery receipt never arrives - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`snowbridge-pallet-outbound-queue-v2` records a `PendingOrder { nonce, fee, block_number }` for every outbound message and only ever resolves it (and pays the associated `fee`/tip) via `process_delivery_receipt`, which is reachable solely through `submit_delivery_receipt` when a relayer supplies a valid Ethereum event-log proof for that nonce. There is no timeout, no permissionless reap path, and no way to refund or otherwise release the fee if no relayer ever submits a matching receipt (message dropped, censored, gateway paused, receipt malformed/unreachable, etc.). This mirrors the reported Trading.sol pattern: a fee is collected/attributed up front and only consumed in one specific success branch, leaving it permanently stuck when that branch is never reached, with no withdrawal/recovery method provided.

### Finding Description
`do_process_message` inserts a `PendingOrder` carrying the message's `fee` for every accepted outbound message, keyed by `nonce`: [1](#0-0) 

The only code path that removes an entry from `PendingOrders` and pays out the `fee` is `process_delivery_receipt`, invoked from the signed extrinsic `submit_delivery_receipt` after verifying an Ethereum event-log proof: [2](#0-1) [3](#0-2) 

If verification never succeeds for a given nonce — because the corresponding Ethereum-side event is never emitted (execution reverted, gateway halted, message dropped by relayers, or the receipt is simply never submitted) — the `PendingOrder` for that nonce stays in storage forever. There is no expiry, no `on_idle`/`on_initialize` sweep, and no admin or user-callable function anywhere in the pallet to cancel a stale order or refund/redirect its `fee`. `AddTip::add_tip` only *increases* the locked amount for an existing pending order; it has no complementary "reclaim" operation: [4](#0-3) 

This is structurally identical to the reported bug: a fee/value is set aside for a single conditional "success" branch (`fulfill{value: msg.value}` in the report vs. `register_reward(..., order.fee)` here), and the code that "just keeps it" when that branch is never reached provides no exit method (no `withdraw` equivalent).

### Impact Explanation
Each unresolved `PendingOrder` represents fee/tip value that is economically committed (accounted for reward purposes) but never actually paid to anyone and never returned to the originator — a permanent fund lock. At scale (many messages that never get delivery receipts, e.g., during a Gateway pause or beacon-client outage on BridgeHub), this becomes a systemic value-lock issue affecting Snowbridge's fee/reward accounting rather than a one-off user mistake, satisfying the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
This does not require a malicious actor, admin abuse, or a compromised relayer — it triggers purely from the absence of a delivery receipt, which can occur for entirely legitimate reasons (Ethereum-side revert, `Verifier`/beacon-client unavailability, Gateway `OperatingMode` halted, or simply no relayer bothering to submit a proof for a low-value message). Given that message delivery to Ethereum is not guaranteed to succeed or be observed on-chain by BridgeHub within any bounded time, encountering at least some permanently orphaned `PendingOrders` is highly likely over the system's lifetime.

### Recommendation
Add a permissionless or time-gated "reap" extrinsic/hook that, after a configurable number of blocks past `PendingOrder.block_number` with no matching receipt, either refunds the fee to the original message sender (if the sender/account is recoverable from the message) or moves it to a recoverable pot (similar in spirit to the `LostTips` mechanism already present in `snowbridge-pallet-system-v2`). At minimum, expose a governance-gated cleanup call so stuck fees are not permanently unrecoverable, and document/bound the expected maximum time a `PendingOrder` can remain unresolved.

### Proof of Concept
1. Submit an outbound message via `snowbridge_pallet_system_v2::Pallet::send` (or the XCM exporter) with a non-zero `fee`, causing `do_process_message` to insert `PendingOrders::<T>::insert(nonce, order)` with that fee attached. [1](#0-0) 
2. Do not submit a corresponding `submit_delivery_receipt` for that nonce (e.g., because the Ethereum-side execution reverts, so the `DeliveryReceipt` event referencing this nonce is never emitted, or no relayer submits the proof).
3. `PendingOrders::<T>::get(nonce)` remains populated indefinitely; there is no other call in the pallet (`Call::submit_delivery_receipt` is the sole entry point) that can remove it or release the associated `fee`. [5](#0-4) 
4. The fee value is permanently unreachable — it was never actually escrowed to a burnable/refundable account tracked outside `PendingOrders`, so no existing pallet function (in this file) can recover it.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-317)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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
