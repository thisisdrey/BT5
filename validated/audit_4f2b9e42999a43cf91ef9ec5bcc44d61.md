### Title
`add_tip` bypasses the halted/paused operating-mode check, letting users burn value while the Snowbridge bridge is halted - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The external report shows `addGasFee` in `CCMPSendMessageFacet.sol` letting users pay/lock fees even when the gateway is paused, causing possible fund loss. The same broken invariant — "pause the primary send/verify path but forget to pause a secondary fee/tip path" — exists in Snowbridge's tip-adding flow: `snowbridge_pallet_system_frontend::Pallet::add_tip` never checks `ExportOperatingMode`/halted state before swapping and burning the user's tip asset, unlike its sibling call `register_token`, which does perform this check.

### Finding Description
`snowbridge-pallet-system-frontend` stores an `ExportOperatingMode` (`Normal`/`Halted`) and exposes `set_operating_mode` for governance to halt exports to Ethereum [1](#0-0) .

`register_token` correctly refuses to run while halted: [2](#0-1) 

But `add_tip`, the sibling extrinsic that also spends user funds (swaps the tip asset for Ether and burns it via `swap_fee_asset_and_burn`, then relays the tip cross-chain via XCM `Transact`), has **no** such check: [3](#0-2) 

The tip is forwarded to BridgeHub's `outbound-queue-v2` pallet, whose `AddTip::add_tip` implementation blindly increases `PendingOrder.fee` with no halted/verifier check at all: [4](#0-3) 

Meanwhile, the actual protection against a halted bridge lives only in `pallet-ethereum-client`'s `Verifier::verify`, which now (per `prdoc/stable2603-2/pr_11856.prdoc`) rejects proof verification when the light client is `Halted`, closing the previously-reported gap for `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt`: [5](#0-4) [6](#0-5) 

That fix only guards the verifier-consuming paths (`submit`/`submit_delivery_receipt`). It does **not** cover the `add_tip` path, which never calls the `Verifier` and never checks any operating mode/halted flag on either `system-frontend` (AssetHub) or `outbound-queue-v2` (BridgeHub). A user can therefore still call `add_tip` while:
- `system-frontend::ExportOperatingMode` is `Halted`, or
- the whole bridge is halted via `pallet-ethereum-client::OperatingMode::Halted` (the "emergency stop" governance uses when the light client is suspected compromised).

The `add_tip` call unconditionally executes `swap_fee_asset_and_burn`, which swaps the user's asset for Ether and burns it for teleport — an irreversible loss of user funds — even though the underlying `PendingOrder` this tip is attached to can never be settled while the bridge is halted (since `submit_delivery_receipt` will now fail `Verifier::verify` with `Halted`).

### Impact Explanation
This matches "permanent user-fund lock/loss" under the impact gate: an unprivileged user can be induced (or can mistakenly) burn real value (swapped Ether) attached to a message/order that cannot be delivered while the bridge is halted, and there is no path to reclaim the burned tip. It does not require a malicious relayer, validator, or governance actor — the root cause is a missing guard on a public, unprivileged, value-destroying entrypoint (`add_tip`), directly analogous to `addGasFee` in the seed report.

### Likelihood Explanation
Likelihood is moderate/low: it only manifests when governance has halted the bridge (an intentional emergency-stop condition), and it requires a user (or someone acting on their behalf, e.g. automated tipping) to submit `add_tip` during that specific window. However, since there is no explicit block on the call, and no visible on-chain indicator forcing wallets/relayer-tools to check halted state before tipping, users can easily lose funds unknowingly during exactly the periods when the bridge is most stressed (compromise/incident response).

### Recommendation
Add a halted check to both ends of the tip pipeline:
- In `snowbridge_pallet_system_frontend::Pallet::add_tip`, add `ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);` before performing `swap_fee_asset_and_burn`, mirroring `register_token`.
- In `snowbridge_pallet_outbound_queue_v2::Pallet`'s `AddTip::add_tip` implementation (and the analogous `AddTip` impl in `inbound-queue-v2`), check the pallet's/bridge's operating mode (or delegate to `T::Verifier`/`pallet-ethereum-client::OperatingMode`) before mutating `PendingOrders`/`Tips`, so that a halted bridge cannot accept new tips regardless of which entrypoint is used.

### Proof of Concept
1. Governance calls `pallet_ethereum_client::set_operating_mode(Halted)` on BridgeHub (emergency stop) — or `snowbridge_pallet_system_frontend::set_operating_mode(Halted)` on AssetHub.
2. A user calls `snowbridge_pallet_system_frontend::add_tip(message_id, tip_asset)` on AssetHub. There is no halted check in this call path [7](#0-6) , so `swap_fee_asset_and_burn` executes, swapping and burning the user's asset.
3. The XCM `Transact` reaches BridgeHub's backend and ultimately `outbound_queue_v2::AddTip::add_tip`, which increments `PendingOrders[nonce].fee` without any halted check [4](#0-3) .
4. Because the bridge is halted, `submit_delivery_receipt` will fail at `T::Verifier::verify` (`VerificationError::Halted`) [5](#0-4) , so the `PendingOrder` (and the tip fee just added/burned) can never be resolved/paid — the user's burned Ether is permanently lost with no compensating settlement.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L254-273)
```rust
		/// Add an additional relayer tip for a committed message identified by `message_id`.
		/// The tip asset will be swapped for ether.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::add_tip()
				.saturating_add(T::BackendWeightInfo::transact_add_tip())
		)]
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

**File:** prdoc/stable2603-2/pr_11856.prdoc (L1-17)
```text
title: 'Snowbridge: halt the Ethereum verifier when the bridge is in emergency stop'

doc:
  - audience: Runtime Dev
    description: |
      When `pallet-ethereum-client` is in `Halted` operating mode, its `Verifier::verify`
      implementation now short-circuits with the new `VerificationError::Halted` instead of
      attempting to verify Ethereum-side proofs.

      Previously, halting the light client only blocked new beacon header updates via
      `EthereumBeaconClient::submit`. Proof verification still ran, which meant
      `inbound_queue_v2::submit` and `outbound_queue_v2::submit_delivery_receipt` could
      continue to process receipts and pay out relayer rewards from `PendingOrders` while
      governance had halted the bridge (e.g. after a suspected beacon light client compromise).

      Halting the verifier closes that gap in one place — covering both inbound dispatch and
      outbound delivery-receipt reward payments.
```
