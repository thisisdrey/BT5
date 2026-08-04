I found a concrete local analog. The user's tip asset is swapped for ether and *burned* on AssetHub by `pallet-snowbridge-system-frontend::add_tip`, then a `Transact` XCM instructs `pallet-snowbridge-system-v2::add_tip` on BridgeHub to credit a relayer reward for a given nonce. If that credit fails (nonce already consumed / message unknown), the burned value is only recorded in `LostTips` — and there is no extrinsic anywhere in the pallet, the runtime, or the wider bridge stack that lets the affected account reclaim it.

### Title
Permanently unrecoverable user funds in `LostTips` with no withdrawal/claim path - ([File: bridges/snowbridge/pallets/system-v2/src/lib.rs])

### Summary
`pallet-snowbridge-system-frontend::add_tip` (AssetHub) burns the user's tip asset for teleportation to Ethereum before dispatching a `Transact` call to `pallet-snowbridge-system-v2::add_tip` (BridgeHub). On BridgeHub, if `InboundQueue::add_tip`/`OutboundQueue::add_tip` fails (e.g. `AddTipError::NonceConsumed` or `UnknownMessage`), the amount is only recorded in the `LostTips` storage map keyed by the sender — real value has already left the user's control (burned on AH, and never actually delivered to any relayer reward on BH), and no dispatchable exists to redeem it back to the user.

### Finding Description
- `swap_fee_asset_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` unconditionally burns the tip asset (via `burn_for_teleport`) before the cross-chain `Transact` even executes: [1](#0-0) 
- `add_tip` then fires a fire-and-forget `Transact` to BH's `EthereumSystem::add_tip`: [2](#0-1) 
- On BridgeHub, `pallet_snowbridge_system_v2::Pallet::add_tip` attempts to attach the tip to the inbound/outbound queue; on failure it only mutates `LostTips`, never reverting or refunding the already-burned value: [3](#0-2) 
- The storage item and its doc comment confirm the intended recovery mechanism was never implemented: "Capturing the lost tips here supports implementing a recovery method in the future." [4](#0-3) 
- The `AddTip` trait and its error variants (`NonceConsumed`, `UnknownMessage`) show this failure path is a normal expected outcome, not an edge case: [5](#0-4) 
- An integration test explicitly exercises and accepts this "stuck" outcome as expected behavior, confirming the design gap is live in the current codebase rather than already patched: [6](#0-5) 

This is structurally identical to the external report's core invariant break: value is taken from a user, a "no way to withdraw" storage bucket accumulates the debt, but the codebase provides no method to redeem/return it, so it is permanently lost.

### Impact Explanation
Any signed AssetHub account whose tip lands on an already-processed nonce, a message not yet indexed, or any other `AddTip` failure has its ether-equivalent value burned with zero possibility of recovery through any public or governance extrinsic present in the repository. This is a direct value-conservation violation ("Balances, assets ... contract-held value must conserve value and settle exactly once to the rightful beneficiary") since burned funds settle to nobody. Because tipping is a normal, user-triggered, permissionless action (races against relayers naturally consuming nonces), this is not a rare edge case — it can be triggered unintentionally by an ordinary unprivileged user under normal bridge congestion/timing, causing real ETH-equivalent loss with no code path to reverse it.

### Likelihood Explanation
High. `NonceConsumed`/`UnknownMessage` are explicit, foreseen error variants of `AddTip`, meaning race conditions between a user submitting a tip and a relayer processing the message are expected, not exceptional. The existing test `tip_to_invalid_nonce_is_added_to_lost_tips` demonstrates this path is trivially reachable by any signed account with no special privileges, and the burn happens unconditionally before the cross-chain outcome is known.

### Recommendation
Add a governed or self-service claim extrinsic (e.g. `claim_lost_tip(origin)` in `pallet-snowbridge-system-v2`) that mints/refunds the recorded `LostTips[sender]` amount back to the sender (or to a designated beneficiary via XCM to AssetHub, mirroring the existing reward payment flow), and clears the entry after payout. Alternatively, avoid the destructive burn on AssetHub until BridgeHub confirms the tip was actually attached (e.g., via a receipt/ack flow), so failure naturally leaves funds recoverable on the origin chain instead of requiring a new redemption pathway.

### Proof of Concept
1. User calls `SnowbridgeSystemFrontend::add_tip(origin, MessageId::Outbound(nonce), asset)` on AssetHub for a `nonce` that is about to be (or already has been) consumed by the relayer processing the corresponding outbound message.
2. `swap_fee_asset_and_burn` burns the asset for teleport immediately: [7](#0-6) 
3. The `Transact` reaches BridgeHub's `EthereumSystem::add_tip`; `OutboundQueue::add_tip(nonce, amount)` returns `Err(AddTipError::NonceConsumed)`.
4. `pallet_snowbridge_system_v2::Pallet::add_tip` catches the error and only records it into `LostTips::<T>::mutate(&sender, ...)`, emitting `TipProcessed { success: false, .. }`: [8](#0-7) 
5. `sender`'s tip value is now permanently gone: it was burned on AssetHub in step 2 and only exists as an unclaimable ledger entry in `LostTips` on BridgeHub, matching the existing integration test `tip_to_invalid_nonce_is_added_to_lost_tips`: [9](#0-8) 
6. Grep confirms no extrinsic, migration, or governance call anywhere in the repository reads/clears/pays out `LostTips` (searched `LostTips` across the whole codebase — only found in the definition, its mutation, and this test's assertion).

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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L32-43)
```rust
#[derive(Debug, Encode, PartialEq, DecodeWithMemTracking, Decode, TypeInfo, PalletError)]
pub enum AddTipError {
	NonceConsumed,
	UnknownMessage,
	AmountZero,
}

/// Trait to add a tip for a nonce.
pub trait AddTip {
	/// Add a relayer reward tip to a pallet.
	fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError>;
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
