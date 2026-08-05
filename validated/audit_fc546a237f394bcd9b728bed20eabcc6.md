This confirms the claim's technical mechanics are accurate. The frontend pallet's `add_tip` (in `snowbridge-pallet-system-frontend`) calls `swap_fee_asset_and_burn`, which irrevocably burns/withdraws the tip asset via `burn_for_teleport` **before** sending the transact call to BridgeHub.Audit Report

## Title
Relayer tips lost forever with no recovery path when `add_tip` fails after nonce consumption - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
`SnowbridgeSystemFrontend::add_tip` on AssetHub irrevocably burns/withdraws the user's tip asset via `swap_fee_asset_and_burn` → `burn_for_teleport` before dispatching a `Transact` XCM to BridgeHub's `EthereumSystemV2::add_tip`. If the target message nonce has already been consumed by the time the XCM lands, `InboundQueue::add_tip`/`OutboundQueue::add_tip` return `AddTipError::NonceConsumed`, and `pallet-snowbridge-system-v2` only records the amount into `LostTips` without any refund, mint, or callback back to the AssetHub sender, and the call still returns `Ok(())`. There is no extrinsic anywhere in the reviewed pallets that reads `LostTips` and pays the depositor back, so the burned value is permanently stranded.

## Finding Description
On AssetHub, `SnowbridgeSystemFrontend::add_tip` (bridges/snowbridge/pallets/system-frontend/src/lib.rs, `add_tip` call and `swap_fee_asset_and_burn`/`swap_and_burn`) withdraws/swaps the user's tip asset and calls `burn_for_teleport`, permanently destroying the asset from the user's balance on AssetHub, then sends an unpaid `Transact` XCM carrying `EthereumSystemCall::AddTip` to BridgeHub. This burn is unconditional and happens before any confirmation that the tip will actually be credited on BridgeHub.

On BridgeHub, `EthereumSystemV2::add_tip` (bridges/snowbridge/pallets/system-v2/src/lib.rs, L251-281) dispatches to `InboundQueue::add_tip`/`OutboundQueue::add_tip`. `InboundQueue::add_tip` (bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs, L248-258) explicitly checks `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` — if the message for that nonce was already processed (an ordinary race between independent relaying and the tip XCM's arrival), the call fails. `add_tip` on BridgeHub does not propagate this failure to the caller: it swallows the error, records the lost amount into `LostTips::<T>` (L136-142, L266-270), emits `TipProcessed { success: false, .. }`, and still returns `Ok(())`.

Because the burn on AssetHub already happened and is irreversible, and because `LostTips` is bookkeeping-only with no corresponding claim/sweep extrinsic in `snowbridge-pallet-system-v2` (confirmed by the storage doc comment stating it only "supports implementing a recovery method in the future"), the user's funds are permanently lost with no path to recovery in the current code.

Existing guards are insufficient: the nonce-consumed check in the queue prevents double-crediting a processed message, which is correct, but the frontend's irreversible burn combined with the backend's silent-failure/no-refund design means the check's failure mode destroys user value rather than safely rejecting the operation.

## Impact Explanation
This matches the "permanent user-fund lock" impact category: a real, user-supplied DOT (or other convertible asset) value is burned on AssetHub and cannot be recovered once the corresponding BridgeHub-side tip-add fails due to nonce consumption. This is not a hypothetical edge case — the codebase's own dedicated tests (`add_tip_inbound_fails_when_nonce_is_consumed`, `tip_to_invalid_nonce_is_added_to_lost_tips`) validate this exact failure path, and no test or code path exists that recovers or reimburses the sender.

## Likelihood Explanation
No privileged action, malicious relayer, or compromised key is required. Any ordinary user calling `SnowbridgeSystemFrontend::add_tip` for a nonce that gets processed by a normal relayer at roughly the same time as the tip XCM arrives on BridgeHub will trigger this loss. Given XCM's inherent asynchronous, non-atomic cross-chain delivery and independent message relaying, this race is a routine occurrence rather than an exotic condition, making the issue realistically and repeatedly triggerable.

## Recommendation
- Do not irrevocably burn/withdraw the tip asset on AssetHub before confirmation of success on BridgeHub; alternatively, escrow it and only burn/settle once success is confirmed.
- On BridgeHub, when `add_tip` fails, implement an actual recovery/refund path: either mint/return an equivalent value back through an XCM message to the original AssetHub sender, or add a permissionless `claim_lost_tip` extrinsic in `pallet-snowbridge-system-v2` that lets `sender` withdraw their `LostTips` balance (crediting them with backed value, not just clearing bookkeeping).
- Ensure any such recovery flow correctly re-mints/unlocks the exact `LostTips` amount to the exact `sender` exactly once.

## Proof of Concept
1. On AssetHub, call `SnowbridgeSystemFrontend::add_tip(origin, MessageId::Inbound(N), asset)` — this burns the tip asset via `swap_fee_asset_and_burn`/`burn_for_teleport` and sends the `AddTip` transact XCM to BridgeHub (bridges/snowbridge/pallets/system-frontend/src/lib.rs, L261-273).
2. Concurrently, an ordinary relayer submits/finalizes the inbound message for nonce `N` on BridgeHub, causing `Nonce::<T>::set(nonce)` (bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs, L225) to mark it consumed before the tip XCM executes.
3. `EthereumSystemV2::add_tip` executes; `InboundQueue::add_tip` returns `AddTipError::NonceConsumed` (bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs, L252), reproduced by the existing unit test `add_tip_inbound_fails_when_nonce_is_consumed` (bridges/snowbridge/pallets/system-v2/src/tests.rs, L197-219) and integration test `tip_to_invalid_nonce_is_added_to_lost_tips` (cumulus/.../snowbridge_v2_outbound.rs, L277-320).
4. The pallet records the amount into `LostTips::<T>::get(sender)` and emits `TipProcessed { success: false, .. }`, returning `Ok(())` — no refund is issued to the AssetHub sender whose asset was already burned.
5. Inspect `snowbridge-pallet-system-v2` for any extrinsic reading/clearing `LostTips` to pay the sender — none exists, confirming the loss is permanent. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L286-317)
```rust

		/// Swaps a specified tip asset to Ether and then burns the resulting ether for
		/// teleportation. Returns the amount of Ether gained if successful, or a DispatchError if
		/// any step fails.
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L277-320)
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
}
```
