Confirmed: `LostTips<T>` in `snowbridge-pallet-system-v2` has no extrinsic to reclaim funds — it is documented as "supports implementing a recovery method in the future," and grep across the repo shows no claim/withdraw call for it.

### Title
`add_tip` on a stale/invalid nonce permanently burns user funds into an unrecoverable `LostTips` entry with no reclaim path - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
The Snowbridge tip flow burns the user's asset on AssetHub *before* it is known whether the corresponding Bridge Hub message (identified by nonce) can still accept a tip. If the nonce has already been consumed (message already processed) or the outbound order no longer exists, `EthereumSystemV2::add_tip` on Bridge Hub silently records the already-burned amount into `LostTips<T>` — a storage map that has no corresponding claim/withdraw extrinsic anywhere in the codebase.

### Finding Description
`pallet-snowbridge-system-frontend::add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`) is a permissionless, signed extrinsic. It immediately calls `swap_fee_asset_and_burn`, which swaps the caller's asset for Ether and burns/teleports it via `burn_for_teleport::<T::AssetTransactor>` [1](#0-0) . This value is irreversibly removed from the user's balance regardless of what happens downstream. The frontend then dispatches an XCM `Transact` to Bridge Hub carrying the already-burned `amount` [2](#0-1) .

On Bridge Hub, `EthereumSystemV2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:251-281`) attempts to forward the tip to the relevant queue pallet's `AddTip::add_tip`: [3](#0-2) 

If that call fails — e.g. `AddTipError::NonceConsumed` (inbound queue, when `Nonce::<T>::get(nonce)` is already true, `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`) or `AddTipError::UnknownMessage` (outbound queue, when `PendingOrders` no longer contains the nonce, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:483-495`) — the pallet does not revert or refund. It simply accumulates the amount into `LostTips<T>`: [4](#0-3) 

The doc-comment on the storage item itself admits the gap: *"Capturing the lost tips here supports implementing a recovery method in the future"* [5](#0-4) . A repo-wide search for any extrinsic reading/withdrawing `LostTips` finds none — the map is written-only.

This is the structural analog of the audited bug: a public entry point (`optOut`/`add_tip`) can be invoked freely, and the accounting variable it inflates (`optedOut`/`LostTips`) is never validated against a "can this actually still be settled" precondition at the time value is committed — the state that determines success (whether the nonce/order is still open) is checked only after the economically-irreversible action (burn) has already happened, and the resulting inflated/orphaned value has no corrective path.

### Impact Explanation
Any user who calls `add_tip` for a nonce that finishes processing (or is otherwise removed from `PendingOrders`) between transaction construction and Bridge Hub execution — which is a normal race given cross-chain XCM latency between AssetHub and Bridge Hub — has their Ether-equivalent asset burned with no way to recover it. This is a permanent user-fund lock: value is destroyed by `burn_for_teleport` on AssetHub, but the compensating credit (`LostTips`) on Bridge Hub is dead storage with no extraction mechanism. Given `add_tip` is a normal, expected, permissionless user operation (not requiring any privileged/relayer/validator action), this can affect any ordinary relayer-tipping user under ordinary race conditions.

### Likelihood Explanation
High likelihood of occurring in normal operation (not an attack, but a race-condition/design gap): the window between a user submitting `add_tip` on AssetHub and the XCM `Transact` executing on Bridge Hub is exactly the window during which the targeted inbound nonce can be consumed by a relayer, or the targeted outbound order can be completed/pruned. No malicious actor, governance action, or privileged role is required — an honest relayer processing the message promptly is sufficient to trigger the loss.

### Recommendation
- Short term: Add a `claim_lost_tip` (or similar) extrinsic that lets the `sender` recorded in `LostTips<T>` reclaim their lost amount (e.g., minted back as native Ether/DOT equivalent, or via a compensating XCM credit back to their AssetHub account).
- Alternatively, restructure the flow so the burn only happens after Bridge Hub confirms the nonce/order is still open (e.g., two-phase: reserve on AssetHub, burn only on confirmed success, refund on failure) rather than burn-then-hope-it-lands.
- Long term: add integration tests asserting that `LostTips` balances are eventually recoverable, and fuzz/property tests around the AssetHub/Bridge Hub message race window.

### Proof of Concept
1. User calls `SnowbridgeSystemFrontend::add_tip(origin, message_id=Inbound(N), asset)` on AssetHub. `swap_fee_asset_and_burn` burns the user's asset immediately [6](#0-5) .
2. Before the resulting XCM `Transact` executes on Bridge Hub, a relayer submits the inbound message for nonce `N`, causing `Nonce::<T>::set(N)` in `process_message` [7](#0-6) .
3. The XCM `Transact` now executes `EthereumSystemV2::add_tip`, which calls `InboundQueue::add_tip(N, amount)`, hits `ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed)` and returns `Err` [8](#0-7) .
4. `add_tip` on Bridge Hub catches this error branch and writes the amount into `LostTips::<T>::mutate(&sender, ...)` [9](#0-8) , exactly matching the existing test `add_tip_inbound_fails_when_nonce_is_consumed` / `tip_to_invalid_nonce_is_added_to_lost_tips` [10](#0-9) [11](#0-10) .
5. The user's asset is gone (burned on AssetHub in step 1); `LostTips` on Bridge Hub records the amount but no extrinsic exists anywhere in the codebase to redeem it — the funds are permanently locked.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-351)
```rust
		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-271)
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
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L219-226)
```rust
			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

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
