All claims in the report are verified against the actual code. The burn happens unconditionally in `swap_fee_asset_and_burn` before the XCM `Transact` is even sent to Bridge Hub [1](#0-0) , and the burn itself (via `burn_for_teleport`) is irreversible [2](#0-1) . On Bridge Hub, `add_tip` only writes to `LostTips` on failure with no compensating mint/refund path [3](#0-2) , and the storage doc-comment itself confirms no recovery mechanism currently exists [4](#0-3) . A repo-wide grep for `LostTips` confirms it is referenced only in the pallet's write path, its tests, and one integration test — no claim/withdraw extrinsic exists anywhere in the codebase.

Audit Report

## Title
`add_tip` on a stale/invalid nonce permanently burns user funds into an unrecoverable `LostTips` entry with no reclaim path - (File: bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
The Snowbridge tip flow burns the user's asset on AssetHub via `swap_fee_asset_and_burn` before it is known whether the corresponding Bridge Hub message (identified by nonce) can still accept a tip. If the nonce has already been consumed or the outbound order no longer exists, `EthereumSystemV2::add_tip` on Bridge Hub silently records the already-burned amount into `LostTips<T>`, a storage map with no corresponding claim/withdraw extrinsic anywhere in the codebase.

## Finding Description
`pallet-snowbridge-system-frontend::add_tip` is a permissionless, signed extrinsic that immediately calls `swap_fee_asset_and_burn`, which burns/teleports the caller's asset via `burn_for_teleport::<T::AssetTransactor>` [5](#0-4) . This value is irreversibly removed from the user's balance regardless of what happens downstream. The frontend then dispatches an XCM `Transact` to Bridge Hub carrying the already-burned `amount` via `build_add_tip_call`/`send_transact_call` [6](#0-5) .

On Bridge Hub, `EthereumSystemV2::add_tip` forwards the tip to the relevant queue pallet's `AddTip::add_tip` [7](#0-6) . If that call fails — e.g. `AddTipError::NonceConsumed` when `Nonce::<T>::get(nonce)` is already true (inbound queue), or `AddTipError::UnknownMessage` when `PendingOrders` no longer contains the nonce (outbound queue) — the pallet does not revert or refund; it simply accumulates the amount into `LostTips<T>` [8](#0-7) . The storage doc-comment itself admits the gap: "Capturing the lost tips here supports implementing a recovery method in the future" [4](#0-3) . A repo-wide search for any extrinsic reading/withdrawing `LostTips` confirms none exists — the map is write-only.

## Impact Explanation
Any user who calls `add_tip` for a nonce that finishes processing (or is otherwise removed from `PendingOrders`) between transaction construction and Bridge Hub execution — a normal race given cross-chain XCM latency between AssetHub and Bridge Hub — has their Ether-equivalent asset burned with no way to recover it. This is a permanent user-fund lock: value is destroyed by `burn_for_teleport` on AssetHub, while the compensating credit (`LostTips`) on Bridge Hub is dead storage with no extraction mechanism. Since `add_tip` is a normal, permissionless user operation requiring no privileged action, this affects any ordinary relayer-tipping user under ordinary race conditions, matching the "permanent user-fund lock" impact category.

## Likelihood Explanation
High likelihood in normal operation: the window between a user submitting `add_tip` on AssetHub and the XCM `Transact` executing on Bridge Hub is exactly the window during which the targeted inbound nonce can be consumed by a relayer, or the targeted outbound order can be completed/pruned. No malicious actor, governance action, or privileged role is required — an honest relayer processing the message promptly is sufficient to trigger the loss, and this is directly demonstrated by the existing tests `add_tip_inbound_fails_when_nonce_is_consumed` and `tip_to_invalid_nonce_is_added_to_lost_tips`.

## Recommendation
- Short term: Add a `claim_lost_tip` (or similar) extrinsic allowing the `sender` recorded in `LostTips<T>` to reclaim their lost amount (e.g., minted back as native Ether/DOT equivalent, or via a compensating XCM credit back to their AssetHub account).
- Alternatively, restructure the flow so the burn only happens after Bridge Hub confirms the nonce/order is still open (two-phase: reserve on AssetHub, burn only on confirmed success, refund on failure) rather than burn-then-hope-it-lands.
- Long term: add integration tests asserting that `LostTips` balances are eventually recoverable, and fuzz/property tests around the AssetHub/Bridge Hub message race window.

## Proof of Concept
1. User calls `SnowbridgeSystemFrontend::add_tip(origin, message_id=Inbound(N), asset)` on AssetHub; `swap_fee_asset_and_burn` burns the user's asset immediately [1](#0-0) .
2. Before the resulting XCM `Transact` executes on Bridge Hub, a relayer submits the inbound message for nonce `N`, causing `Nonce::<T>::set(nonce)` in `process_message`.
3. The XCM `Transact` executes `EthereumSystemV2::add_tip`, which calls `InboundQueue::add_tip(N, amount)`, hits the nonce-consumed check, and returns `Err`.
4. `add_tip` on Bridge Hub catches this error and writes the amount into `LostTips::<T>::mutate(&sender, ...)` [8](#0-7) , matching the existing test `add_tip_inbound_fails_when_nonce_is_consumed` and integration test `tip_to_invalid_nonce_is_added_to_lost_tips`.
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
