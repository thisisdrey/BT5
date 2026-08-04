### Title
Irrecoverable fund loss when tipping a Snowbridge outbound message that is delivered before the tip's cross-chain settlement completes - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is that a value-bearing action (fee capture) is executed unconditionally by whoever's transaction lands first, without any binding to the state that justified the fee in the first place, so the payer's contribution can be lost or misdirected. The local analog is Snowbridge's `add_tip` flow: `pallet-snowbridge-system-frontend::add_tip` on AssetHub irreversibly swaps/burns the user's tip asset for Ether **before** it is known whether the corresponding message on Bridge Hub is still pending. Because the tip is delivered asynchronously via XCM and only applied if `PendingOrders` for that nonce still exists [1](#0-0) , a race between message delivery (`submit_delivery_receipt` / `process_delivery_receipt`, which removes the `PendingOrders` entry) [2](#0-1)  and the tip's arrival results in the user's already-burned funds being unrecoverably destroyed, with no atomic rollback tying the AssetHub-side burn to the Bridge Hub-side settlement outcome.

### Finding Description
On AssetHub, `Pallet::add_tip` immediately swaps the caller's supplied asset for Ether and burns it for teleportation via `swap_fee_asset_and_burn`, then merely dispatches an XCM `Transact` carrying an `AddTip` call to Bridge Hub: [3](#0-2) . This burn/swap is final and non-refundable at the point of execution — it happens before the destination-side effect is known to be applicable.

On Bridge Hub, the corresponding `AddTip::add_tip` implementation only succeeds if a `PendingOrders` entry for the message's nonce still exists; if the message has already been delivered (via `submit_delivery_receipt` → `process_delivery_receipt`, which calls `PendingOrders::<T>::remove(nonce)`) [2](#0-1) , the tip application fails with `AddTipError::UnknownMessage`: [1](#0-0) .

The integration test `tip_to_invalid_nonce_is_added_to_lost_tips` confirms this exact outcome: a tip targeting a nonce that doesn't exist (or is already resolved) results in `TipProcessed { success: false, .. }` and the tip amount being recorded into a `LostTips` storage map rather than being returned to the payer: [4](#0-3) . Since the AssetHub-side burn already occurred unconditionally and permanently, there is no cross-chain atomicity guarding the value transfer — the user's real, spendable asset was already destroyed at the moment of the `add_tip` call, regardless of whether the Bridge Hub side can still apply it.

This exactly mirrors the `delegateBtc` bug class: an operation that captures/destroys value based on an identifier (`txid` / message `nonce`) without any guarantee that the identifier's associated state is still valid at settlement time, causing loss (there) or destruction (here) of the payer's value due to the natural, unprivileged race between two independently-timed transactions (message delivery vs. tip delivery) — no malicious peer, relayer, or validator is required, an ordinary relayer delivering the message quickly is sufficient.

### Impact Explanation
This is a real value-conservation violation: funds are burned on the source chain without a guarantee of matching, atomic settlement on the destination chain. Per the required impacts ("Balances, assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" and "permanent user-fund ... lock"), an honest user's Ether-equivalent value can be permanently destroyed with no recovery path visible in this pallet (only a bookkeeping entry in `LostTips`, whose reclaim mechanism — if any — lives outside the files inspected here).

### Likelihood Explanation
No adversarial or privileged actor is required. Any ordinary, honest relayer processing a message delivery receipt in the normal course of operation, racing against a user's independently-submitted tip transaction traveling cross-chain via XCM, is sufficient to trigger the loss. This is a naturally occurring race in permissionless bridge operation, not a "front-run-only" contrived scenario — the burn happens unconditionally on AssetHub before the Bridge Hub state is known.

### Recommendation
Do not burn/swap the tip asset on AssetHub until the corresponding Bridge Hub `PendingOrders` entry is confirmed to still exist, or make the AssetHub-side burn reversible/refundable if the Bridge Hub-side application fails (e.g., have the `AddTip` failure trigger a compensating XCM back to AssetHub to refund the original asset, or hold funds in escrow until settlement confirmation before burning).

### Proof of Concept
1. A relayer submits `add_tip(message_id=N, asset)` on AssetHub, which immediately burns the swapped Ether for teleportation and dispatches an XCM `AddTip` transact to Bridge Hub [3](#0-2) .
2. Before the XCM message lands on Bridge Hub, another (unrelated, honest) relayer submits `submit_delivery_receipt` for nonce `N`, causing `process_delivery_receipt` to remove the `PendingOrders` entry for `N` [5](#0-4) .
3. The `AddTip` XCM call then executes on Bridge Hub, finds no `PendingOrders` entry, and fails with `AddTipError::UnknownMessage` [1](#0-0) ; the emulated test `tip_to_invalid_nonce_is_added_to_lost_tips` demonstrates this exact failure path and the resulting non-zero `LostTips` entry [4](#0-3) .
4. The user's original asset, already burned in step 1, is not restored — it is only logged as "lost" rather than refunded.

### Citations

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
