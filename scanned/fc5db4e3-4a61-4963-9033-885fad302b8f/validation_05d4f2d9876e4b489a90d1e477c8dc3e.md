Based on my investigation, I found a concrete local analog of the reported bug pattern in Snowbridge's `pallet-system-frontend` / `pallet-system-v2` tip flow: user funds are irrevocably burned/swapped on the sender chain (AssetHub) *before* the corresponding credit on BridgeHub is guaranteed to succeed, and when it fails, the value is only recorded in a `LostTips` counter with no recovery path implemented — a permanent, non-privileged user-fund lock.

### Title
`EthereumSystemFrontend::add_tip` burns the user's tip asset before BridgeHub crediting is guaranteed, permanently locking funds in `LostTips` with no recovery path - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`EthereumSystemFrontend::add_tip` swaps the caller's asset to Ether and irreversibly burns it via `burn_for_teleport` on AssetHub, then sends an XCM `Transact` to BridgeHub's `EthereumSystemV2::add_tip` to credit the relayer reward for a given `message_id`/nonce. If that downstream credit fails (e.g., the referenced nonce was already consumed by the time the message arrives), BridgeHub does not re-mint or return the Ether — it only increments `LostTips<T>` for the sender, a value with no claim/withdrawal extrinsic in the current codebase.

### Finding Description
The flow is:
1. On AssetHub, `EthereumSystemFrontend::add_tip` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`) calls `swap_and_burn`, which swaps the user's asset for Ether and then calls `burn_for_teleport::<T::AssetTransactor>` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:287-317`). This **irrevocably destroys** the user's funds on AssetHub before any confirmation that the tip will be applied.
2. It then sends an XCM `Transact` carrying `EthereumSystemCall::AddTip { sender, message_id, amount }` to BridgeHub.
3. On BridgeHub, `EthereumSystemV2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:251-282`) dispatches to `InboundQueue::add_tip`/`OutboundQueue::add_tip`. If the nonce has already been consumed (a legitimate, permissionless-triggerable race — a relayer can always submit and consume a nonce before/concurrent with a user's tip transaction), `add_tip` fails with `AddTipError::NonceConsumed` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`).
4. Crucially, `add_tip` in `pallet-system-v2` **swallows this error** and returns `Ok(())` from the extrinsic regardless (`bridges/snowbridge/pallets/system-v2/src/lib.rs:266-280`), only bumping `LostTips::<T>::mutate(&sender, ...)` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:140-142`, `268-270`).
5. `LostTips` is documented explicitly as unrecoverable today: *"Capturing the lost tips here supports implementing a recovery method in the future"* (`bridges/snowbridge/pallets/system-v2/src/lib.rs:136-139`). No such recovery extrinsic exists in this codebase.

This exactly mirrors the reported bug class: value is destroyed/removed from circulation (burn) on one side of a two-step accounting operation, while the corresponding credit is conditionally skipped, and the shortfall is tracked in an inert counter instead of being returned to the rightful owner or the pool that should absorb it. Here it is not "the remaining stakers" absorbing the loss, but the sender's Ether simply vanishing from total issuance with only a bookkeeping stub remaining.

### Impact Explanation
This is a straightforward, permissionless fund-loss/fund-lock path: any unprivileged AssetHub user calling `add_tip` for a message that gets processed (consuming its nonce) before, or concurrently with, their tip transaction lands on BridgeHub will have their tip asset swapped and burned with no recourse. The integration test `tip_to_invalid_nonce_is_added_to_lost_tips` (`cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:277-320`) demonstrates this exact scenario is reachable and produces `LostTips > 0` with no compensating mint or credit — i.e., real, unbacked destruction of user value, matching the "permanent user-fund or bridge-state lock" and "theft or unbacked mint/unlock" (inverse: unbacked burn) impact categories in the gate.

### Likelihood Explanation
No malicious peer, relayer, validator, or governance actor is required. Nonce consumption races naturally: relayers are economically incentivized to submit inbound/outbound proofs as fast as possible, and a user's tip is added for a `message_id` that references a nonce which can be processed independently and concurrently by any relayer. The window is realistic for busy nonces (the exact case the tip feature — "reward topups" for under-priced messages, PR `prdoc/stable2506/pr_8271.prdoc` — exists to address), making this triggerable in normal operation without any attacker action beyond a normal `add_tip` call at an unlucky time.

### Recommendation
Do not burn the tip asset on AssetHub before the corresponding credit is confirmed on BridgeHub. Options: (a) hold/escrow the swapped Ether on AssetHub (or mint a claim ticket) until a delivery-receipt/ack confirms the tip was applied, only then burning for teleport; or (b) implement the promised recovery extrinsic that lets `LostTips` be re-minted/refunded to the original sender's account, and gate `burn_for_teleport` on a successful reservation rather than unconditional burn-then-hope.

### Proof of Concept
The existing test already demonstrates the vulnerable path end-to-end: [1](#0-0) 
It calls `SnowbridgeSystemFrontend::add_tip` with a real DOT asset for a nonce that is never valid (`MessageId::Outbound(22)`), which triggers `swap_and_burn` (irreversible burn on AssetHub) and results in `TipProcessed { success: false }` plus `LostTips > 0` on BridgeHub — with no subsequent step in the codebase that returns or credits that value back to the `relayer` account. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L287-317)
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-282)
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
