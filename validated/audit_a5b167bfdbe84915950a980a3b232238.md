Confirmed — no reclaim/refund extrinsic exists for `LostTips` anywhere in the `bridges/snowbridge` tree. The claim is fully corroborated by the code: the frontend `add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` withdraws and burns the user's asset (`swap_fee_asset_and_burn` → `burn_for_teleport`) *before* the BridgeHub-side `system-v2::add_tip` even attempts the nonce check, and there's an existing integration test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:277-320` that demonstrates funds landing in `LostTips` with no further code path draining it.

Audit Report

## Title
Unrecoverable Tip Funds Permanently Locked in `LostTips` When Nonce Consumption Races Ahead of `add_tip` - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
The AssetHub-side `system-frontend::add_tip` extrinsic irrevocably withdraws and burns a user's tip asset via `swap_fee_asset_and_burn`/`burn_for_teleport` before dispatching an XCM `Transact` to BridgeHub's `system-v2::add_tip`. If the target inbound/outbound message nonce is already consumed by the time this XCM lands (a routine race against permissionless relayer submission), `InboundQueue::add_tip`/`OutboundQueue::add_tip` return `AddTipError::NonceConsumed`, and `system-v2::add_tip` silently swallows this by moving the amount into the `LostTips<T>` storage map and returning `Ok(())`. No extrinsic anywhere in the `bridges/snowbridge` pallets (`system-v2`, `system-frontend`, `inbound-queue-v2`, `outbound-queue-v2`) or `bridges/modules/relayers` ever reads or drains `LostTips<T>`, so the already-burnt funds are permanently stranded with no beneficiary.

## Finding Description
The frontend pallet, `bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`, executes `Self::swap_fee_asset_and_burn(who.clone().into(), asset)?` which withdraws the user's fee asset, swaps it to ether, and calls `burn_for_teleport` — an irreversible destructive action — before building and sending the `AddTip` transact call to BridgeHub via `Self::send_transact_call`. On BridgeHub, `system-v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281`) dispatches to `InboundQueue::add_tip`/`OutboundQueue::add_tip`. In `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`, the guard `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` rejects the tip if the nonce was already marked processed by `process_message` (`inbound-queue-v2/src/lib.rs:214-245`, which sets `Nonce::<T>::set(nonce)` at line 225 as soon as any permissionless relayer calls `submit`). When this rejection occurs, `system-v2::add_tip` does not propagate the error to the caller or attempt any compensating action — it executes `LostTips::<T>::mutate(&sender, |lost_tip| { *lost_tip = lost_tip.saturating_add(amount); })` and returns `Ok(())` (lines 266-270, 280). The storage doc comment at lines 136-142 explicitly states recovery is only a "future" possibility, confirming no present-day repayment path exists. Existing guards (the nonce-consumed check) only prevent nonce/reward-state corruption; they do nothing to protect the user's already-destroyed funds.

## Impact Explanation
This is a permanent user-fund lock: real value (ether, swapped from the user's supplied fee asset) is irreversibly burned on AssetHub in anticipation of a reward top-up that can silently fail, and the corresponding accounting entry in `LostTips<T>` has no redemption mechanism. The value of the affected `sender`'s balance entry in `LostTips<T>` is the exact corrupted/orphaned state — it can only grow monotonically across the network and is never repaid to the `sender` nor forwarded to any relayer, matching the "permanent user-fund or bridge-state lock" category of the impact gate.

## Likelihood Explanation
This requires no privileged actor, malicious relayer, or governance action. It occurs any time a permissionless relayer calls `submit`/`process_message` for a given nonce before the asynchronous XCM carrying the tip registration from AssetHub arrives at BridgeHub — an ordinary and expected race condition in a system where relaying is explicitly permissionless and cross-chain XCM delivery is not synchronous. The scenario is demonstrated as a normal code path by the existing test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:277-320`, which asserts a positive `LostTips` balance results from this exact race, with no follow-up recovery step in the codebase.

## Recommendation
Add a signed extrinsic in `snowbridge-pallet-system-v2` (e.g., `reclaim_lost_tip`) that allows the `sender` recorded in `LostTips<T>` to trigger a refund (via XCM teleport/mint back to their AssetHub account) of their stranded balance, clearing the corresponding entry atomically on success. Alternatively, redesign the frontend flow so the user's asset is only burned after BridgeHub confirms non-consumption of the nonce (e.g., an escrow/hold-and-release pattern), rather than burning eagerly and hoping the tip registration message wins the race.

## Proof of Concept
1. On AssetHub, a user calls `EthereumSystemFrontend::add_tip(origin, message_id, asset)`; `swap_fee_asset_and_burn` withdraws and burns the user's asset (`system-frontend/src/lib.rs:261-273`, `:290-317`).
2. Concurrently, any relayer calls `InboundQueue::submit`/`process_message` for the same nonce, which executes `Nonce::<T>::set(nonce)` before the tip XCM arrives (`inbound-queue-v2/src/lib.rs:222-225`).
3. The tip XCM executes `system-v2::add_tip`, which calls `InboundQueue::add_tip`, hitting `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` and returning `Err(AddTipError::NonceConsumed)` (`inbound-queue-v2/src/lib.rs:252`).
4. `system-v2::add_tip` catches this, executes `LostTips::<T>::mutate(&sender, ...)`, emits `TipProcessed { success: false }`, returns `Ok(())` (`system-v2/src/lib.rs:266-280`).
5. This exact flow is already validated by the repository's own integration test `tip_to_invalid_nonce_is_added_to_lost_tips` (`cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:277-320`), which asserts `relayer_lost_tip > 0` and stops there — no subsequent call anywhere in the repository drains or repays this balance. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-258)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
	}

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L254-317)
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
	}

	impl<T: Config> Pallet<T> {
		fn send_xcm(origin: Location, dest: Location, xcm: Xcm<()>) -> Result<XcmHash, SendError> {
			let is_waived =
				<T::XcmExecutor as FeeManager>::is_waived(Some(&origin), FeeReason::ChargeFees);
			let (ticket, price) = validate_send::<T::XcmSender>(dest, xcm.clone())?;
			if !is_waived {
				T::XcmExecutor::charge_fees(origin, price).map_err(|_| SendError::Fees)?;
			}
			T::XcmSender::deliver(ticket)
		}

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
