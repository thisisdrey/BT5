### Title
Optimistic Fee Burn Before Cross-Chain Confirmation Causes Permanent Loss of User Funds in `add_tip` - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet-snowbridge-system-frontend::add_tip` on Asset Hub irrevocably burns the caller's tip asset *before* it knows whether the corresponding `add_tip` action on Bridge Hub will actually succeed. The BH-side action can fail for reasons entirely outside the caller's control (message already delivered/consumed), in which case the failure is only recorded as bookkeeping in `LostTips` — the burned value is never refunded or re-created anywhere. This is the same class of bug as the UMA report: value is taken from a user to fund an action (reward top-up) on the assumption that a second, separate step will consume it, but that second step can independently fail/no-op, leaving the withdrawn funds permanently stranded/destroyed instead of reaching their intended beneficiary.

### Finding Description
On Asset Hub, `Pallet::add_tip` [1](#0-0)  does:
1. `swap_fee_asset_and_burn` — swaps the user's supplied asset for Ether and calls `burn_for_teleport`, permanently destroying the user's tokens on Asset Hub [2](#0-1) .
2. Builds an `AddTip` transact call and fires it to Bridge Hub via `UnpaidExecution` XCM (fire-and-forget, no confirmation is awaited) [3](#0-2) .

On Bridge Hub, `snowbridge_pallet_system_v2::Pallet::add_tip` dispatches to either the inbound or outbound queue's `AddTip::add_tip`, and if that call errors, it *only* records the amount into `LostTips` (a bookkeeping map, not a refund) and emits `TipProcessed{success: false}` — no value is minted, returned, or credited to anyone [4](#0-3) .

The `AddTip::add_tip` implementations fail deterministically whenever the referenced nonce/order no longer exists:
- Outbound: `PendingOrders` entry already removed (order already resolved via `submit_delivery_receipt`) → `AddTipError::UnknownMessage` [5](#0-4) .
- Inbound: nonce already processed → `AddTipError::NonceConsumed` [6](#0-5) .

This is confirmed by the existing test `tip_to_invalid_nonce_is_added_to_lost_tips`, which shows the tip is simply parked in `LostTips` with no path back to the user [7](#0-6) .

Because message relay/delivery and tip submission are asynchronous and race-prone (a relayer can submit `submit_delivery_receipt` for a message at almost any time), a normal, honest, unprivileged user can trigger this loss simply by tipping a message that gets delivered/consumed slightly before the tip's cross-chain `Transact` executes — no malicious relayer, governance, or privileged actor is required.

### Impact Explanation
User funds (real Ether-denominated value, already burned/destroyed on Asset Hub) are permanently lost with no reward, refund, or recovery mechanism whenever the corresponding Bridge Hub state has already advanced (nonce consumed / order resolved) by the time the tip's XCM `Transact` executes. This is a direct value-conservation violation: the pivot requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" is violated because the source-chain burn is not gated on, nor reversible after, failure of the destination-chain settlement step.

### Likelihood Explanation
No privileged actor, malicious relayer, or governance action is needed. Any ordinary Asset Hub user calling `add_tip` for a message that is close to being (or has just been) delivered/confirmed by any honest relayer will hit this race and lose their tip value. Given messages are actively being delivered continuously by the relayer network, this is a realistically frequent occurrence, not a contrived edge case.

### Recommendation
Do not burn/destroy the tip asset before the Bridge Hub `add_tip` action is known to succeed. Options:
- Hold/escrow the tip asset on Asset Hub (or in a reserve/sovereign account) and only finalize the burn (or route to reward payout) once BH confirms success, using a callback/receipt mechanism (similar to how message delivery receipts settle rewards).
- Alternatively, have the BH-side failure path trigger a refund XCM back to the AH tipper (mint-and-teleport back), rather than merely logging into `LostTips`.
- At minimum, expose `LostTips` via a claimable extrinsic that lets affected users recover value, and reflect this in the AH-side flow to avoid asserting the burn as final before settlement.

### Proof of Concept
1. User calls `EthereumSystemV2::add_tip` via `pallet-snowbridge-system-frontend::add_tip(message_id = Outbound(nonce_N), asset)` on Asset Hub.
2. `swap_fee_asset_and_burn` executes and irrevocably burns the user's tip asset (converted to Ether) via `burn_for_teleport` [2](#0-1) .
3. Before the resulting `UnpaidExecution` XCM `Transact` is executed on Bridge Hub, a relayer submits `submit_delivery_receipt` for nonce `N`, causing `process_delivery_receipt` to remove the `PendingOrders` entry for `N` [8](#0-7) .
4. The delayed `AddTip{nonce: N}` XCM now executes on Bridge Hub; `OutboundQueue::add_tip` returns `AddTipError::UnknownMessage` because the order no longer exists [5](#0-4) .
5. `snowbridge_pallet_system_v2::add_tip` catches this error and only updates `LostTips`, emitting `TipProcessed{success:false}` [9](#0-8) .
6. Result: the user's burned Ether value is permanently gone — never delivered as a reward top-up, never refunded to the user, and only recorded as an unclaimable bookkeeping entry, exactly matching the "value taken from account but not delivered to its intended recipient" pattern from the UMA `LongShortPair`/`LongShortPairCreator` report, this time manifesting as outright destruction of user funds instead of trapped-in-contract funds.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L353-363)
```rust
		fn build_remote_xcm(call: &impl Encode) -> Xcm<()> {
			Xcm(vec![
				DescendOrigin(T::PalletLocation::get()),
				UnpaidExecution { weight_limit: Unlimited, check_origin: None },
				Transact {
					origin_kind: OriginKind::Xcm,
					call: call.encode().into(),
					fallback_max_weight: None,
				},
			])
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-476)
```rust
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
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
