Audit Report

## Title
`add_tip` bypasses the halted/paused operating-mode check, letting users burn value while the Snowbridge bridge is halted - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`snowbridge_pallet_system_frontend::Pallet::add_tip` swaps a user's asset for Ether and irrevocably burns it via `swap_fee_asset_and_burn`/`burn_for_teleport` without ever checking `ExportOperatingMode`, unlike its sibling `register_token`, which explicitly guards on `!Self::export_operating_mode().is_halted()` [1](#0-0) [2](#0-1) . The same missing guard exists downstream in `snowbridge_pallet_outbound_queue_v2::Pallet`'s `AddTip::add_tip` implementation, which mutates `PendingOrders` fee without any halted/operating-mode check [3](#0-2) .

## Finding Description
`ExportOperatingMode` storage and `set_operating_mode` exist to let governance halt exports to Ethereum [4](#0-3) . `register_token` respects this by calling `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted)` before doing any fund-affecting work [5](#0-4) . `add_tip`, which performs the identical value-destroying operation (`swap_fee_asset_and_burn`, then relays via XCM `Transact`), has no equivalent check [2](#0-1) .

On the BridgeHub side, `outbound_queue_v2::AddTip::add_tip` blindly increases `PendingOrder.fee` for any nonce, again without checking any operating mode [3](#0-2) . The only halted-state protection in the pipeline is `pallet-ethereum-client::Verifier::verify`, invoked from `submit_delivery_receipt`, which now rejects proof verification when halted [6](#0-5) [7](#0-6) . That guard covers `submit_delivery_receipt`/`submit`, but it is never invoked by `add_tip` on either the frontend or backend pallet, so the burn happens unconditionally regardless of halted state.

## Impact Explanation
This is a genuine inconsistency between two sibling extrinsics that perform the same class of fund-destructive operation: `register_token` is halt-aware, `add_tip` is not. An unprivileged, signed user calling `add_tip` while the bridge is halted (`ExportOperatingMode::Halted` on the frontend, or the underlying light client `Halted` via `pallet-ethereum-client::OperatingMode`) will still have their asset swapped and burned via `burn_for_teleport`, an irreversible operation, attached to a message whose settlement (`submit_delivery_receipt`) is blocked for as long as the halt persists. This matches the "public underpriced work / permanent user-fund lock" pattern in the impact gate: a public, unprivileged entrypoint destroys real value with no halted-state guard, mirroring the missing-pause-check pattern described in the seed report.

## Likelihood Explanation
Exploitability requires governance to have placed the bridge into `Halted` mode — an emergency-stop condition that is not attacker-controlled, so likelihood is moderate/low. However, no code path prevents a user (or tooling/automation acting for them) from calling `add_tip` during that window, and there is no on-chain signal forcing callers to check the halted flag first, so unintended fund loss can occur exactly when the bridge is under stress (e.g. suspected light-client compromise), which is a realistic and repeatable condition once triggered.

## Recommendation
Add halted checks mirroring `register_token`:
- In `snowbridge_pallet_system_frontend::Pallet::add_tip`, add `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` before calling `swap_fee_asset_and_burn`.
- In `snowbridge_pallet_outbound_queue_v2::Pallet`'s `AddTip::add_tip` implementation (and the analogous implementation in `inbound-queue-v2`), check the pallet's/bridge's operating mode before mutating `PendingOrders`, so a halted bridge cannot accept new tips through either entrypoint.

## Proof of Concept
1. Governance halts the bridge via `pallet_ethereum_client::set_operating_mode(Halted)` or `snowbridge_pallet_system_frontend::set_operating_mode(Halted)`.
2. A user calls `add_tip(message_id, tip_asset)` on the frontend pallet — no halted check exists in this path [2](#0-1) , so `swap_fee_asset_and_burn` executes and burns the swapped Ether.
3. The XCM `Transact` reaches `outbound_queue_v2::AddTip::add_tip`, which increments `PendingOrders[nonce].fee` unconditionally [3](#0-2) .
4. While halted, `submit_delivery_receipt` fails at `T::Verifier::verify` with `VerificationError::Halted` [6](#0-5) , so the `PendingOrder` (and the attached burned tip) cannot be resolved for the duration of the halt, demonstrating the missing guard's fund-loss exposure.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L190-208)
```rust
	/// The current operating mode for exporting to Ethereum.
	#[pallet::storage]
	#[pallet::getter(fn export_operating_mode)]
	pub type ExportOperatingMode<T: Config> = StorageValue<_, OperatingMode, ValueQuery>;

	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: Into<Location>,
	{
		/// Set the operating mode for exporting messages to Ethereum.
		#[pallet::call_index(0)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_operating_mode(origin: OriginFor<T>, mode: OperatingMode) -> DispatchResult {
			ensure_root(origin)?;
			ExportOperatingMode::<T>::put(mode);
			Self::deposit_event(Event::ExportOperatingModeChanged { mode });
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-231)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-30)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
```
