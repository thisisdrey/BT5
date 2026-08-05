Based on my review, the claim is accurate and directly verifiable in the code.

Audit Report

## Title
Permanently unrecoverable user funds in `LostTips` with no withdrawal/claim path - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

## Summary
`pallet-snowbridge-system-frontend::add_tip` on AssetHub irrevocably burns the user's tip asset via `swap_fee_asset_and_burn`/`burn_for_teleport` before the corresponding `Transact` XCM even executes on BridgeHub. If `pallet-snowbridge-system-v2::add_tip` on BridgeHub then fails to attach the tip to the inbound/outbound queue (`AddTipError::NonceConsumed` or `UnknownMessage`), the amount is only recorded in the `LostTips` storage map, and no extrinsic anywhere in the pallet, runtime, or bridge stack allows the affected account to reclaim it.

## Finding Description
`add_tip` in [1](#0-0)  calls `swap_fee_asset_and_burn`, which unconditionally destroys the tip asset via `burn_for_teleport` (or swap-then-burn) before any cross-chain confirmation occurs, as seen in [2](#0-1) . The resulting `Transact` call reaches BridgeHub's `EthereumSystem::add_tip`, implemented in [3](#0-2) , which on failure only writes to `LostTips::<T>` via `saturating_add` and emits `TipProcessed { success: false, .. }` — it never reverts, refunds, or re-mints the value. The storage doc comment at [4](#0-3)  explicitly states that "Capturing the lost tips here supports implementing a recovery method in the future," confirming no such recovery mechanism currently exists. The `AddTip` trait's error variants `NonceConsumed` and `UnknownMessage` (in `bridges/snowbridge/primitives/core/src/reward.rs`) are explicit, foreseeable outcomes of races between tip submission and relayer processing, not rare edge cases. A grep across the repository confirms `LostTips` is referenced only in its definition, its mutation on failure, and a test assertion (`tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`) — there is no call index, migration, or governance call anywhere that reads or clears `LostTips`.

## Impact Explanation
This is a direct value-conservation violation: an unprivileged, ordinary signed account can have real value burned on AssetHub with no corresponding beneficiary ever receiving it on BridgeHub, and the ledger entry in `LostTips` is permanently stranded with zero code path to redeem it. This matches the "permanent user-fund lock" impact category, since funds are destroyed and settle to nobody, and no privileged or unprivileged extrinsic exists to make the affected account whole.

## Likelihood Explanation
High. Triggering this requires no privilege or attacker sophistication — a normal user submitting `add_tip` for a nonce that a relayer is concurrently/already processing (or hasn't yet been indexed) hits the exact failure path exercised by the existing test. Since tipping is a permissionless, common user action and the burn happens unconditionally before the outcome of the cross-chain call is known, this is readily and repeatably reachable under normal bridge operation/timing, not a contrived scenario.

## Recommendation
Add a claim/refund extrinsic (e.g., `claim_lost_tip(origin)`) in `pallet-snowbridge-system-v2` that pays out `LostTips[sender]` back to the sender (directly or via XCM back to AssetHub) and clears the entry afterward. Alternatively, restructure the flow so the burn on AssetHub only occurs after BridgeHub confirms successful tip attachment (e.g., via an acknowledgment/receipt flow), leaving funds recoverable on the origin chain when attachment fails, rather than requiring a new redemption mechanism.

## Proof of Concept
1. User calls `SnowbridgeSystemFrontend::add_tip(origin, MessageId::Outbound(nonce), asset)` on AssetHub for a nonce about to be, or already, consumed by a relayer.
2. `swap_fee_asset_and_burn` immediately burns the asset (`burn_for_teleport`), as shown in [5](#0-4) .
3. The `Transact` XCM reaches BridgeHub; `OutboundQueue::add_tip(nonce, amount)` returns `Err(AddTipError::NonceConsumed)`.
4. `pallet_snowbridge_system_v2::Pallet::add_tip` records the loss into `LostTips::<T>` and emits `TipProcessed { success: false, .. }`, per [6](#0-5) .
5. The sender's tip is permanently gone — burned on AssetHub, unclaimable on BridgeHub — matching the integration test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`, which asserts `relayer_lost_tip > 0` as expected behavior with no follow-up recovery call.

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```
