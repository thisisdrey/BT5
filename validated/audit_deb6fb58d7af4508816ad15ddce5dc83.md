This confirms the claim fully. In `pallet_system_frontend::add_tip`, the user's fee asset is withdrawn and irrevocably burned via `swap_fee_asset_and_burn` → `burn_for_teleport::<T::AssetTransactor>` at [1](#0-0)  before the `AddTip` XCM `Transact` call is even sent to BridgeHub. Once burned, the value is permanently gone from the user's account on Asset Hub with no possibility of it being returned by AssetHub-side logic. The XCM `Transact` then invokes `pallet_system_v2::add_tip` on BridgeHub at [2](#0-1) , which — if `InboundQueue::add_tip`/`OutboundQueue::add_tip` fails (stale/consumed nonce or missing pending order) — merely records the amount into `LostTips::<T>` via `LostTips::<T>::mutate` at lines 268-270, emits `TipProcessed { success: false }`, and still returns `Ok(())`.

The integration test `tip_to_invalid_nonce_is_added_to_lost_tips` at [3](#0-2)  demonstrates this exact end-to-end path: real Ether is burned on Asset Hub, a nonexistent nonce is targeted on BridgeHub, and the test explicitly asserts `relayer_lost_tip > 0` in the `LostTips` map with no subsequent claim mechanism. The storage doc comment at [4](#0-3)  itself acknowledges no recovery method currently exists. I confirmed via `grep_search` for `LostTips` across the repo that only `lib.rs`, `tests.rs`, and the outbound integration test reference this storage item — no extrinsic, migration, or hook reads and pays it out anywhere in the codebase.

This validates the claim as described: a real value-conservation defect where user-paid tips are burned/spent unconditionally but the corresponding reward-accounting can fail, permanently stranding funds in a bookkeeping-only map.

Audit Report

## Title
Relayer tips are burned but permanently lost with no recovery path when `add_tip` targets a stale/invalid nonce - `LostTips` accounting is a dead end - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
`pallet_system_frontend::add_tip` withdraws and permanently burns the user's tip asset via `swap_fee_asset_and_burn`/`burn_for_teleport` on Asset Hub before forwarding an `AddTip` XCM `Transact` call to BridgeHub. If `pallet_system_v2::add_tip` on BridgeHub finds the targeted nonce/order already consumed or nonexistent, it does not fail the extrinsic; it only credits the amount to `LostTips<T>`, a storage map with no reader anywhere in the codebase, and returns `Ok(())`. This is a real, tested (including in end-to-end integration tests) permanent-fund-loss condition, not a contrived edge case.

## Finding Description
The full flow is: `pallet_system_frontend::add_tip` (Asset Hub) calls `Self::swap_fee_asset_and_burn(who.clone().into(), asset)` which either swaps the tip asset for Ether and burns it, or directly burns it via `burn_for_teleport::<T::AssetTransactor>` — see `bridges/snowbridge/pallets/system-frontend/src/lib.rs:267,372-404`. This burn is irreversible and happens unconditionally before any confirmation that the tip will actually be attached to a real reward. The pallet then builds and sends an XCM `Transact` containing `EthereumSystemCall::AddTip` to BridgeHub (`send_transact_call`, lines 406-423).

On BridgeHub, `pallet_system_v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281`) dispatches to `InboundQueue::add_tip` or `OutboundQueue::add_tip` depending on `message_id`. If that call errors (e.g., nonce already consumed, or pending order not found), the error is swallowed: the code adds `amount` to `LostTips::<T>` keyed by `sender` (lines 266-271), emits `TipProcessed { success: false, .. }`, and unconditionally returns `Ok(())` (line 280). There is no code path anywhere in the pallet, `pallet_system_frontend`, or the wider bridge-hub/asset-hub runtimes that reads from or pays out `LostTips`. The storage doc comment (lines 136-139) explicitly states recovery is only "supported... in the future," confirming no such mechanism currently exists.

Existing guards do not prevent this: `FrontendOrigin::ensure_origin` in `system_v2::add_tip` only authenticates the calling origin, not the validity of the nonce/order; the inner `AddTip::add_tip` implementations correctly detect and reject invalid nonces, but by the time that check runs, the corresponding value has already been irrevocably burned on Asset Hub in a separate, already-committed transaction.

## Impact Explanation
This is a permanent user-fund lock: value that was withdrawn and burned from a legitimate user's account to incentivize relaying can never be recovered once the tip fails to attach to a valid nonce/order, because the only record of it (`LostTips`) has no claim, sweep, or governance-triggered payout mechanism anywhere in the repository. This matches the "permanent user-fund... lock" category of the impact gate. The amount at risk is bounded only by whatever tip size a user or frontend chooses to attach, so losses can be non-trivial per occurrence.

## Likelihood Explanation
This requires no privileged or malicious actor — it is a normal race condition in the intended tipping workflow: a user (or automated relaying agent) adds a tip for a message that has, in the interim, already been processed by a relayer (nonce consumed) or whose pending order has expired/been fulfilled. This is explicitly covered by unit tests (`add_tip_inbound_fails_when_nonce_is_consumed`, `add_tip_outbound_fails_when_pending_order_not_found`) and by the full end-to-end integration test `tip_to_invalid_nonce_is_added_to_lost_tips`, which burns real Ether on Asset Hub and confirms `LostTips::<Runtime>::get(relayer) > 0` on BridgeHub with no further recourse. Given tipping timing is inherently racy against relayer processing, this is a realistically frequent occurrence under normal, non-adversarial operation.

## Recommendation
Add a signed extrinsic (e.g., `claim_lost_tip(origin)`) that reads and zeroes `LostTips::<T>::take(&who)` and mints/transfers the equivalent value back to the caller through whatever asset-issuance mechanism is appropriate for Ether-denominated tips on BridgeHub/Asset Hub. Alternatively, restructure the flow so nonce/order validity is verified on BridgeHub (or synchronously reflected back to Asset Hub) before the tip asset is burned on Asset Hub, ensuring a failed tip attempt never actually debits the user. At minimum, stop describing `LostTips` as supporting a "future recovery method" until such a method is implemented and tracked.

## Proof of Concept
1. On Asset Hub, call `pallet_system_frontend::add_tip(origin, message_id, asset)` for a `message_id` referencing a nonce that will be stale/invalid by the time it's processed on BridgeHub — this burns the tip asset immediately via `swap_fee_asset_and_burn`.
2. The forwarded XCM `Transact` invokes `pallet_system_v2::add_tip` on BridgeHub, which calls into `InboundQueue::add_tip`/`OutboundQueue::add_tip`, fails due to the stale nonce, and the amount is added to `LostTips::<T>` with `TipProcessed { success: false }` emitted; the extrinsic still returns `Ok(())`.
3. This exact scenario is reproduced by the existing integration test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:277-319`, and unit tests `add_tip_inbound_fails_when_nonce_is_consumed`/`add_tip_outbound_fails_when_pending_order_not_found` in `bridges/snowbridge/pallets/system-v2/src/tests.rs`.
4. Searching the repository for any extrinsic/migration/hook reading `LostTips` confirms none exists — the burned funds recorded in step 2 are permanently unreachable by the `sender`.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-403)
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
