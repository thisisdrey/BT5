Audit Report

## Title
`PendingOrders` fee never released or reclaimed if delivery receipt is never submitted — permanent lock of relayer reward in Snowbridge outbound queue v2 - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The `outbound-queue-v2` pallet stores a `PendingOrder{nonce, fee, block_number}` for every outbound message in `do_process_message`, and the only code path that removes the order and pays the relayer reward is `process_delivery_receipt`, reachable only via `submit_delivery_receipt` after a valid Ethereum delivery receipt is proven. [1](#0-0) [2](#0-1)  There is no expiry, timeout, or permissionless/governance sweep for stale orders, so if no relayer ever submits the receipt, the order (and its `fee` accounting entry) stays in storage indefinitely with no code path to resolve it. [3](#0-2) 

## Finding Description
`do_process_message` decodes the queued `Message`, builds the `OutboundMessage`/merkle leaf, and inserts a `PendingOrder` keyed by `nonce` carrying the message's `fee` value, explicitly noting the fee is only released once a relayer submits delivery proof. [4](#0-3)  The only removal path is `process_delivery_receipt`, invoked from `submit_delivery_receipt` (the pallet's only dispatchable besides internal calls) after verifying an Ethereum proof via `T::Verifier::verify`. [5](#0-4) [2](#0-1)  The pallet exposes no other extrinsic, hook, `on_idle`, or `on_initialize` sweep that inspects `order.block_number` against any expiry bound to reclaim or reallocate stale entries — confirmed by reading the full pallet module, whose `Call` enum contains only `submit_delivery_receipt`. [6](#0-5) [7](#0-6) 

However, I could not fully verify within the index the underlying custody model for `fee`: whether the number stored in `PendingOrders` represents real tokens already withdrawn/locked into pallet-controlled storage/escrow at message-acceptance time, or whether it is merely an accounting figure that is credited via `T::RewardPayment::register_reward` (from `bp_relayers::RewardLedger`) into a separate reward ledger (`pallet-bridge-relayers`) without any pallet-level custody of funds in `outbound-queue-v2` itself. [8](#0-7)  The `send_message_impl.rs`/`validate` and `deliver` functions for this pallet do not perform any balance withdrawal, reservation, or asset custody themselves — they only validate payload size and enqueue the message. [9](#0-8)  The actual asset withdrawal for the fee (e.g., `WithdrawAsset`/`PayFees` for the remote WETH fee) happens upstream in the XCM `ExportMessage` processing on Asset Hub / the exporter, per the design doc, which is outside this pallet's file. [10](#0-9)  Whether that upstream withdrawal results in funds being escrowed specifically inside `outbound-queue-v2`'s control (and thus genuinely "locked" if the order is never resolved) versus already being consumed/burned/teleported into a separate WETH agent pot on Ethereum (in which case the "lock" is really just an un-incremented reward ledger entry, not a stuck balance) could not be conclusively determined from the available index.

## Impact Explanation
If confirmed that the fee value corresponds to already-escrowed funds under this pallet's or the bridge's direct custody, an order whose receipt never arrives permanently strands that value with no extrinsic, hook, or governance call capable of reclaiming or redirecting it, matching the "permanent user-fund or bridge-state lock" impact category. The `PendingOrders` map only shrinks via `process_delivery_receipt`, and stale entries accumulate indefinitely, consuming storage and leaving fee accounting unresolved. [11](#0-10) 

## Likelihood Explanation
This requires no attacker action — it occurs purely from a relayer never submitting a valid delivery receipt, which is plausible during extended verifier halts, gateway changes, or economically unattractive deliveries. The existing test `poc_m1` demonstrates that a halted verifier causes `submit_delivery_receipt` to fail while the `PendingOrder` remains untouched, and no other test in the suite exercises any recovery path, consistent with none existing. [12](#0-11) 

## Recommendation
Add a permissionless or governance-gated expiry/reclaim path for `PendingOrders`, using the already-stored `block_number` against a configurable bound (mirroring `pallet-treasury`'s `expire_at`/`check_status` pattern) so that fees tied to orders whose receipts are never submitted can be refunded, reassigned, or swept into a reusable pot rather than remaining permanently unresolved. [13](#0-12) 

## Proof of Concept
1. `do_process_message` inserts `PendingOrders::<T>::insert(nonce, PendingOrder{nonce, fee: F, block_number: N})` for a message with nonzero fee. [1](#0-0) 
2. No relayer submits `submit_delivery_receipt` for that `nonce` (e.g., verifier halted, as reproduced in `poc_m1`). [12](#0-11) 
3. `PendingOrders::<T>::get(nonce)` continues to return `Some(order)` indefinitely; `process_delivery_receipt` is the only function capable of consuming/removing it. [14](#0-13) 
4. No other call exists to resolve the order (`Call` enum exposes only `submit_delivery_receipt`), leaving `fee` orphaned in pallet accounting. [5](#0-4) 

**Note on confidence**: the claim's mechanism (no expiry path for `PendingOrders`) is verified as accurate in the code. The severity level ("permanent user-fund lock" vs. "unresolved accounting entry with no direct custody impact") depends on whether the `fee` figure corresponds to funds actually escrowed under bridge control versus already-disbursed/burned value tracked only for reward-ledger bookkeeping purposes — this custody detail could not be fully resolved from the indexed code and would require inspecting the XCM `ExportMessage`/agent-fee-collection path on Asset Hub and the `pallet-bridge-relayers` reward ledger implementation in full to confirm definitively.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L44-46)
```rust
//! # Extrinsics
//!
//! * [`Call::submit_delivery_receipt`]: Submit delivery proof
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L268-271)
```rust
	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
```

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-318)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-44)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
}
```

**File:** bridges/snowbridge/docs/v2.md (L94-119)
```markdown
In all cases, $x_1$ should contain the necessary instructions to:

1. Pay fees for local execution using `PaysFees`
2. Obtain WETH for remote delivery fees.

The XCM bridge-router on AH will charge a small fee to prevent spamming BH with bridge messages. This is necessary since
the `ExportMessage` instruction in message $x_2$ will have no execution fee on BH. For a similar reason, we should also
impose a minimum relayer reward of at least the existential deposit 0.1 DOT, which acts as a deposit to stop spamming
messages with 0 rewards.

### Step 5: BH executes message x2

Message $x_2$ is parsed by the `SnowbridgeMessageExporter` in block $n$ with the following effects:

- A bridge command $m$ is committed to binary merkle tree $M_n$.
  - The transferred asset is parsed from `ReserveAssetDeposited` , `WithdrawAsset` or `TeleportedAssetReceived`
    instructions for the local, destination and teleport asset transfer types respectively.
  - The original origin is preserved through the `AliasOrigin` instruction. This will allow us to resolve agents for the
    case of `Transact`.
  - The message exporter must be able to support multiple assets and reserve types in the same message and potentially
    multiple `Transacts`.
  - The Message Exporter must be able to support multiple Deposited Assets.
  - The Message Exporter must be able to parse `SetAssetClaimer` and allow the provided location to claim the assets on
    BH in case of errors.
- Given relayer reward $r$ in WETH, set storage $P(\mathrm{hash}(m)) = r$. This is parsed from the `WithdrawAsset` and
  `PayFees` instruction within `ExportMessage`.
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
