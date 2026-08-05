The claim is accurate and verified against the code. The `add_tip` extrinsic in `snowbridge-pallet-system-v2` and the frontend's `swap_and_burn`/`burn_for_teleport` flow irrevocably withdraw and burn the user's asset on AssetHub *before* the corresponding BridgeHub `add_tip` call succeeds or fails, and when it fails (e.g., `AddTipError::NonceConsumed`), the value is only recorded in `LostTips` with no extrinsic anywhere in the codebase to reclaim it.This confirms `LostTips` exists only in `snowbridge-pallet-system-v2` with no reader/claim call anywhere else in the codebase — the storage is written to but never read for any recovery purpose.Audit Report

## Title
Relayer tips lost forever with no recovery path when `add_tip` fails after nonce consumption - (`File: bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
The `add_tip` extrinsic in `snowbridge-pallet-system-v2` accepts a tip for an in-flight inbound/outbound message and forwards it to the target queue's reward accounting via `T::InboundQueue::add_tip` / `T::OutboundQueue::add_tip`. If the queue rejects the tip (e.g., `AddTipError::NonceConsumed` because the message was already processed by the time the tip arrives), the pallet does not revert, refund, or return the value — it only records the amount in `LostTips`, a storage map explicitly documented as existing only to "support implementing a recovery method in the future," which does not exist anywhere in the codebase.

## Finding Description
On AssetHub, `snowbridge-pallet-system-frontend::add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs` L261-273) irrevocably swaps the user's provided asset for ether and burns it via `swap_fee_asset_and_burn` → `swap_and_burn` → `burn_for_teleport::<T::AssetTransactor>` (L290-317) *before* dispatching a transact call to BridgeHub. This burn is unconditional and final regardless of what happens on the BridgeHub side.

On BridgeHub, `EthereumSystemV2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs` L251-281) then attempts `InboundQueue::add_tip(nonce, amount)` or `OutboundQueue::add_tip(nonce, amount)`. In `snowbridge-pallet-inbound-queue-v2`, `add_tip` explicitly checks `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` L248-258) — if the message was already processed (nonce consumed), the call errors out.

When this error occurs, `EthereumSystemV2::add_tip` still returns `Ok(())` to the caller (no `DispatchError` is surfaced), and only records the lost value:
```rust
if let Err(ref e) = result {
    LostTips::<T>::mutate(&sender, |lost_tip| {
        *lost_tip = lost_tip.saturating_add(amount);
    });
}
```
`LostTips` is documented as merely "capturing the lost tips here supports implementing a recovery method in the future" (L136-142), confirming no such recovery mechanism currently exists. A grep across the full repository confirms `LostTips` is only ever written to in `system-v2/src/lib.rs` and referenced in tests (`system-v2/src/tests.rs`, `snowbridge_v2_outbound.rs`) — there is no extrinsic anywhere that reads or drains this map back to the user.

The unit test `add_tip_inbound_fails_when_nonce_is_consumed` (`system-v2/src/tests.rs` L197-219) and the integration test `tip_to_invalid_nonce_is_added_to_lost_tips` (`snowbridge_v2_outbound.rs` L277-319) both confirm the outcome is silent bookkeeping of a stranded value with a `TipProcessed { success: false }` event, not a refund or transaction revert.

This is an ordinary, unprivileged race condition: any AssetHub user calling `add_tip` for a message nonce that a relayer independently and legitimately finalizes slightly before the tip's XCM arrives on BridgeHub triggers permanent loss of the burned asset value, with no adversarial or privileged action required.

## Impact Explanation
This matches the "permanent user-fund lock" impact category: real value (burned/teleported ether originating from a user-supplied asset via XCM from AssetHub) becomes permanently unrecoverable from the depositor's perspective due to a structural gap — a missing sweep/claim mechanism for the `LostTips` map — not because of any malicious or privileged action. The loss is triggered purely by normal asynchronous cross-consensus message timing.

## Likelihood Explanation
High feasibility and low barrier: no special privileges, malicious peers, or compromised infrastructure are required. Any legitimate relayer successfully delivering/finalizing an inbound or outbound message around the same time an ordinary user submits `add_tip` for that same nonce from AssetHub reproduces the bug. Given XCM's inherent asynchrony between AssetHub and BridgeHub and independent relaying, this is a foreseeable, repeatable occurrence in normal bridge operation, not a rare edge case.

## Recommendation
- On `add_tip` failure in `EthereumSystemV2::add_tip`, refund the tip back to `sender` via a mint/transfer instead of only bookkeeping it in `LostTips`.
- Alternatively, implement the documented "recovery method": add a permissionless `claim_lost_tip` extrinsic allowing `sender` to withdraw their recorded `LostTips` balance.
- Ensure `snowbridge-pallet-system-frontend::add_tip` only irrevocably burns/teleports the tip asset after confirming (or with a rollback path for) failure on the BridgeHub backend, or holds the value in an account provably swept by a recovery mechanism.

## Proof of Concept
1. A user calls `SnowbridgeSystemFrontend::add_tip` on AssetHub for inbound/outbound message nonce `N`, which swaps and burns their asset for ether (`bridges/snowbridge/pallets/system-frontend/src/lib.rs` L261-273, L290-317) before any confirmation from BridgeHub.
2. Independently, a relayer finalizes the message for nonce `N`, causing `Nonce::<T>::set(nonce)` to mark it consumed on BridgeHub before the tip's XCM transact call arrives.
3. `EthereumSystemV2::add_tip` executes on BridgeHub; `InboundQueue::add_tip` returns `AddTipError::NonceConsumed` per the check in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` L248-258, as unit-tested in `add_tip_inbound_fails_when_nonce_is_consumed` (`system-v2/src/tests.rs` L197-219).
4. The pallet does not revert or refund: it moves `amount` into `LostTips::<T>::get(sender)` and emits `TipProcessed { success: false, .. }` (`system-v2/src/lib.rs` L266-278).
5. No extrinsic in `snowbridge-pallet-system-v2`, `snowbridge-pallet-system-frontend`, or elsewhere in the reviewed code reads `LostTips` to repay the sender — the already-burned value is permanently stranded, confirmed by the integration test `tip_to_invalid_nonce_is_added_to_lost_tips` (`snowbridge_v2_outbound.rs` L277-319). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L290-317)
```rust
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L197-219)
```rust
#[test]
fn add_tip_inbound_fails_when_nonce_is_consumed() {
	new_test_ext(true).execute_with(|| {
		let origin = make_xcm_origin(FrontendLocation::get());
		let sender: AccountId = Keyring::Alice.into();
		// In `MockOkInboundQueue`, the mocked implementation returns an error when the nonce is
		// equal to 3, to simulate an error condition.
		let message_id = MessageId::Inbound(FAILING_NONCE);
		let amount = 1000;

		assert_ok!(EthereumSystemV2::add_tip(origin, sender.clone(), message_id.clone(), amount));

		System::assert_last_event(RuntimeEvent::EthereumSystemV2(Event::<Test>::TipProcessed {
			sender: sender.clone(),
			message_id,
			amount,
			success: false,
		}));

		let lost_tip = LostTips::<Test>::get(sender);
		assert_eq!(lost_tip, 1000);
	});
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L277-319)
```rust
#[test]
pub fn tip_to_invalid_nonce_is_added_to_lost_tips() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	set_up_eth_and_dot_pool();
	let relayer = AssetHubWestendSender::get();

	AssetHubWestend::fund_accounts(vec![(relayer.clone(), INITIAL_FUND)]);

	// A nonce that does not exist.
	let tip_message_id = MessageId::Outbound(22);

	let dot = Location::new(1, Here);
	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::SnowbridgeSystemFrontend::add_tip(
			RuntimeOrigin::signed(relayer.clone()),
			tip_message_id.clone(),
			xcm::prelude::Asset::from((dot, 1_000_000_000u128)),
		));
	});

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		let events = BridgeHubWestend::events();
		assert!(
			events.iter().any(|event| matches!(
				event,
				RuntimeEvent::EthereumSystemV2(snowbridge_pallet_system_v2::Event::TipProcessed { sender, message_id, success, ..})
					if *sender == relayer && *message_id == tip_message_id.clone() && !(*success), // expect a failure
			)),
			"tip added event found"
		);

		let relayer_lost_tip = LostTips::<bridge_hub_westend_runtime::Runtime>::get::<
			sp_runtime::AccountId32,
		>(relayer.into());
		// Assert a tip was added to storage.
		assert!(relayer_lost_tip > 0);
	});
```
