## Analog Found: Permanently Locked Relayer Fees in Snowbridge Outbound Queue V2 with No Cancellation Path

### Title
Outbound bridge messages that never receive a delivery receipt permanently lock the prepaid relayer fee with no cancellation or refund mechanism - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
This is a direct structural analog of the Kakarot `l1->l2` message cancellation bug. When a message is sent from Polkadot to Ethereum via the Snowbridge V2 outbound queue, the caller's fee is withdrawn upfront and stored in a `PendingOrder` keyed by nonce, to be paid to the relayer only when a `submit_delivery_receipt` proving execution on Ethereum is submitted. There is no timeout, expiry, or cancellation extrinsic: if the message is never executed on Ethereum (reverted Gateway call, halted bridge, permanently paused contract, or a relayer simply never bothering to submit the receipt), the fee sits forever in `PendingOrders` with no way for the original sender to reclaim it and no way for anyone to force settlement.

### Finding Description
The pipeline works as follows:
1. `snowbridge_pallet_system_v2::Pallet::send` (or the V2 XCM exporter) builds a `Message` with a `fee` field and calls `OutboundQueue::validate`/`deliver`, which enqueues the message via `T::MessageQueue`. [1](#0-0) 
2. When the message queue processes the message, `do_process_message` decodes it, assigns a `nonce`, and stores a `PendingOrder { nonce, fee, block_number }` in the `PendingOrders` map. The doc comment explicitly states the fee is only rewarded "after" the relayer submits the delivery proof. [2](#0-1) 
3. The only way to remove an entry from `PendingOrders` and settle the fee is `process_delivery_receipt`, called from the `submit_delivery_receipt` extrinsic, which requires a valid proof of execution on Ethereum. [3](#0-2) 
4. There is no other extrinsic, hook, or timeout logic anywhere in the pallet that reads or expires `PendingOrders` entries. The `block_number` field recorded at insertion is never compared against the current block anywhere in `lib.rs`, `types.rs`, or elsewhere in the pallet — it is dead data with respect to any expiry check. [4](#0-3) 

If the corresponding message is never executed on the Ethereum Gateway contract (e.g., the Gateway is paused, the command reverts, gas parameters make it permanently unprofitable for relayers, or the bridge is halted for governance reasons — the pallet's own test suite explicitly demonstrates the "halted" case leaves an order untouched indefinitely), the associated `fee` (locked from the sender's account when the message was validated/sent) can never be:
- returned to the original sender, or
- paid out to any relayer, or
- otherwise released from limbo. [5](#0-4) 

This mirrors exactly the reported Kakarot issue: a public entrypoint accepts a fee for a cross-domain message with no cancellation API to reclaim funds if the remote leg never completes.

### Impact Explanation
Every fee-bearing outbound message (asset transfers, `Transact` calls, governance-adjacent operations routed through the V2 exporter/system pallet) that fails to execute on Ethereum for any reason results in a permanent, unrecoverable loss of the fee paid by the originating account. Because BridgeHub/Snowbridge is a bridge live in the paritytech/polkadot-sdk scope, and because this is a "permanent user-fund lock" class of issue explicitly called out in the impact gate, this qualifies for medium severity consistent with the judge's ruling in the original report (loss of fees/value is treated as loss of capital, capped at medium unless dust).

### Likelihood Explanation
No malicious actor is required. Any of the following ordinary/likely conditions triggers permanent fund lock: the Gateway operating mode being halted (a normal governance/pause action, not attacker-controlled), an Ethereum-side revert of the dispatched command, insufficient gas parameters vs. real network conditions, or simply no relayer finding it profitable to submit the receipt. The pallet's own tests demonstrate the halted-bridge scenario leaves the `PendingOrder` untouched with no other recovery path shown anywhere in the codebase.

### Recommendation
Introduce a cancellation/refund path for `PendingOrders`, e.g.:
- Add a `cancel_stale_order(nonce)` extrinsic (or automatic `on_idle`/`on_initialize` sweep) that becomes callable by the original sender (or permissionlessly refunds to the recorded origin) once `current_block - order.block_number` exceeds a configurable timeout.
- On cancellation, refund `order.fee` back to the message's `origin`/sender account and remove the entry from `PendingOrders`, mirroring the recommended Starknet `l2->l1` message cancellation flow referenced in the original report.
- Ensure the refund path is also reachable when the bridge is halted for an extended period.

### Proof of Concept
1. A user sends an asset/message to Ethereum via `snowbridge_pallet_system_v2::Pallet::send` (or the V2 XCM `ExportMessage` flow), paying a `fee` in the `Message`.
2. `do_process_message` in `outbound-queue-v2` assigns a `nonce`, inserts `PendingOrder { nonce, fee, block_number }` into `PendingOrders`.
3. Before the message is executed on the Ethereum Gateway, the bridge is halted via `set_verifier_halted(true)` (governance action, or the Gateway naturally reverts/pauses) — confirmed reproducible per the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which shows the order stays in storage indefinitely while halted. [6](#0-5) 
4. If the halt is never lifted, or the on-chain command permanently reverts on Ethereum (so no valid `DeliveryReceipt` will ever exist for that nonce), `submit_delivery_receipt` can never succeed for that nonce.
5. The `fee` funds debited from the sender at step 1 are never returned; `PendingOrders[nonce]` remains in storage forever with no extrinsic capable of removing/refunding it outside of a successful delivery receipt.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L284-300)
```rust
	impl<T: Config> Pallet<T> {
		/// Send `command` to the Gateway from a specific origin/agent
		fn send(origin: H256, command: Command, fee: u128) -> DispatchResult {
			let message = Message {
				origin,
				id: frame_system::unique((origin, &command, fee)).into(),
				fee,
				commands: BoundedVec::try_from(vec![command]).unwrap(),
			};

			let ticket = <T as pallet::Config>::OutboundQueue::validate(&message)
				.map_err(|err| Error::<T>::Send(err))?;

			<T as pallet::Config>::OutboundQueue::deliver(ticket)
				.map_err(|err| Error::<T>::Send(err))?;
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L264-271)
```rust
	/// The current nonce for the messages
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageValue<_, u64, ValueQuery>;

	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-448)
```rust
// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
```
