Confirmed: no `withdraw_lost_tips`, `reclaim`, or `redeem`-style extrinsic exists anywhere in `snowbridge-pallet-system-v2` or its callers for the `LostTips` storage item. This is a direct local analog to the USSD "lack of redeem feature" bug class: real value is irreversibly burned from a user's balance, credited into a storage counter that the pallet's own comment says is only meant "to support implementing a recovery method in the future" — and that recovery method was never shipped.

### Title
Permanently unrecoverable user funds in `LostTips` storage with no redemption/withdrawal extrinsic - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

### Summary
`pallet_system_frontend::add_tip` on AssetHub swaps a user's asset for Ether and **burns it for teleport** (`swap_fee_asset_and_burn`) before dispatching the tip to BridgeHub. On BridgeHub, `snowbridge_pallet_system_v2::add_tip` (lib.rs:251-281) attempts to credit that Ether amount to the relayer reward for a given inbound/outbound nonce via `InboundQueue::add_tip` / `OutboundQueue::add_tip`. If that fails (nonce already consumed, order not found, etc.), the amount is added to `LostTips::<T>` (lib.rs:140-142, 266-271) instead of being refunded to the sender. There is no extrinsic, storage migration, or governance call anywhere in the repository that reads and pays out `LostTips` — it is a write-only storage map.

### Finding Description
The comment on the storage item itself documents the intended, but missing, feature:
```
/// Relayer reward tips that were paid by the user to incentivize the processing of their
/// message, but then could not be added to their message reward (e.g. the nonce was already
/// processed or their order could not be found). Capturing the lost tips here supports
/// implementing a recovery method in the future.
#[pallet::storage]
pub type LostTips<T: Config> = StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
```
This mirrors the USSD whitepaper describing a redeem function that was never implemented — here the pallet explicitly documents a "recovery method" that "supports implementing... in the future" but which does not exist in the codebase. Before this failure path is even reached, the actual value has already left the user's control: `swap_fee_asset_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` (372-403) either swaps the tip asset for Ether and burns it, or burns it directly via `burn_for_teleport`, permanently destroying the asset on the local chain in anticipation of it being credited as a reward on the Ethereum side. Once `add_tip` on BridgeHub fails to attribute this value to a reward (attacker-triggerable simply by tipping a nonce that is already consumed or unknown — no privileged actor needed, confirmed by the test `tip_to_invalid_nonce_is_added_to_lost_tips`), the value is stuck forever in `LostTips`.

### Impact Explanation
Any unprivileged AssetHub user who calls `add_tip` for a stale/consumed/nonexistent nonce (or who races a legitimate tip against normal nonce consumption) irrecoverably loses the swapped/burned asset value. Because no call exists to read out and refund/pay `LostTips`, this constitutes a permanent user-fund lock — squarely within the "Required Impacts" bucket: "permanent user-fund or bridge-state lock." It does not require a malicious relayer, validator, or governance actor; it is triggerable by ordinary use (including accidental double-tipping or tipping just after a message is finally processed), and the loss is monotonically increasing and unbounded in aggregate.

### Likelihood Explanation
High. The failure path is exercised by existing unit and emulated tests (`add_tip_inbound_fails_when_nonce_is_consumed`, `add_tip_outbound_fails_when_pending_order_not_found`, `tip_to_invalid_nonce_is_added_to_lost_tips`), proving the condition is easy to hit in normal operation — a relayer or user simply needs to tip a nonce that has already been settled or was never valid, which can happen from ordinary timing/race conditions between message processing and tip submission, not just malicious intent.

### Recommendation
Either (a) validate nonce/order existence and reject `add_tip` before the frontend burns the asset, refunding atomically on failure, or (b) implement the documented "recovery method": an extrinsic (permissioned to the original `sender`, or auto-refund via XCM back to the originating chain/account) that reads and zeroes `LostTips::<T>::get(&sender)` and settles it back to the user. Until such a redemption path exists, `LostTips` should not silently accumulate un-redeemable burned value.

### Proof of Concept
1. On AssetHub, call `EthereumSystemFrontend::add_tip(origin=signed(Alice), message_id=Inbound(N), asset)` where `N` is a nonce that is already fully processed (or does not exist yet on BridgeHub).
2. `swap_fee_asset_and_burn` burns/swaps Alice's asset for Ether and teleports the value cross-chain (see `bridges/snowbridge/pallets/system-frontend/src/lib.rs:267,372-403`).
3. On BridgeHub, `EthereumSystemV2::add_tip` calls `InboundQueue::add_tip(N, amount)`, which returns `Err` because the nonce is already consumed/not found.
4. The pallet executes `LostTips::<T>::mutate(&sender, |lost_tip| *lost_tip = lost_tip.saturating_add(amount));` (lib.rs:268-270) and emits `TipProcessed { success: false }`.
5. Alice's asset value is now recorded only inside `LostTips` — grep across the repository confirms no call path (`withdraw_lost_tips`, `reclaim`, `claim`, or otherwise) ever reads this map to pay it back to Alice. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-143)
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
