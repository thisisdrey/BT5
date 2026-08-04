### Title
Snowbridge relayer-tip flow permanently burns user funds with no recovery path when the remote `add_tip` fails - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`pallet-snowbridge-system-frontend::add_tip` burns the caller's asset on the local chain (AssetHub) *before* the corresponding remote `EthereumSystem::add_tip` call on BridgeHub is guaranteed to succeed. If that remote call fails (e.g. the message nonce was already consumed by the time the tip Transact arrives), the burned value is only logged into `LostTips` on BridgeHub — a storage map with no corresponding claim/refund extrinsic. This mirrors the reported Option-contract pattern of "assume the delta belongs to the caller, with no per-user accounting and no way to recover excess/failed deposits": real value is destroyed unconditionally while the credit side of the transaction is only best-effort.

### Finding Description
`add_tip` on the frontend pallet unconditionally swaps and **burns** the user's supplied asset before any confirmation that the tip will actually be applied: [1](#0-0) 

The burn happens in `swap_fee_asset_and_burn`, which calls `burn_for_teleport` to irrevocably withdraw/destroy the asset from the caller's account: [2](#0-1) 

The resulting `ether_gained` amount is then packaged into an `EthereumSystemCall::AddTip` and sent via an unpaid, fire-and-forget XCM `Transact` to BridgeHub: [3](#0-2) 

On BridgeHub, `pallet-snowbridge-system-v2::add_tip` attempts to apply the tip to the referenced Inbound/Outbound message via `AddTip::add_tip`. This call is inherently racy: an ordinary, unprivileged relayer can process the message (consuming its nonce, or removing its `PendingOrders` entry) between the moment the user submits the tip on AssetHub and the moment the XCM Transact executes on BridgeHub — no malicious actor is required, this is normal, permissionless message processing on the other queue. In that case, `InboundQueue::add_tip` returns `AddTipError::NonceConsumed`/`AddTipError::UnknownMessage` (see `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` `add_tip`, and `outbound-queue-v2/src/lib.rs` `process_delivery_receipt`/`AddTip::add_tip`), and the frontend's `add_tip` call on the system-v2 pallet records the failure into `LostTips` instead of reverting or refunding: [4](#0-3) 

`LostTips` is documented as merely a placeholder for a *future* recovery mechanism that does not currently exist: [5](#0-4) 

No extrinsic in `snowbridge-pallet-system-v2` (or `system-frontend`) reads from or clears `LostTips` to refund the sender. The value that was burned on AssetHub (real Ether/DOT-swapped-to-Ether) is gone: it was never credited to any relayer reward (the tip attach failed) and it is not returned to the original payer. This is exactly the "Option contract" defect pattern: the accounting is based on an optimistic, unconditioned side-effect (burn now, credit later) with no per-transaction atomicity and no way for the affected user to recover their deposit.

### Impact Explanation
This is a permanent, unrecoverable loss of user funds (a real asset burn) triggered purely by normal, unprivileged, non-malicious usage — a benign race between a user's `add_tip` and a relayer's routine message processing. This maps directly to the "Impacts" gate category "permanent user-fund or bridge-state lock": funds are destroyed on the source chain with no compensating mint anywhere and no built-in remediation path, unlike the atomic decode→dispatch→execute→settle requirement stated in the pivots.

### Likelihood Explanation
No adversarial capability, governance, or privileged action is required. Any user calling `add_tip` on a message that is close to being processed/finalized by an honest relayer, or on a message whose fee/order data has since been pruned, will trigger this outcome. Given that message processing races are routine and expected in cross-chain bridge operation, this is a realistically frequent occurrence rather than a contrived edge case.

### Recommendation
Make the burn-and-tip operation atomic and reversible across the two chains:
- Do not burn/withdraw the tip asset on the frontend chain until BridgeHub has confirmed the tip was actually attached (e.g., use a two-phase commit: reserve/hold on AH, only irrevocably burn after a success acknowledgement from BH), or
- Add a claim/refund extrinsic that lets the original `sender` recorded in `LostTips` reclaim (mint back) an equivalent amount, gated by proof that their tip attach failed, and wire that into the existing `PaymentProcedure`/`PayAccountOnLocation` infra already used for relayer rewards.

### Proof of Concept
1. User `Alice` on AssetHub calls `EthereumSystemFrontend::add_tip(origin=Alice, message_id=Inbound(nonce=N), asset=(DOT, X))`.
2. `swap_fee_asset_and_burn` swaps `X` DOT for Ether and calls `burn_for_teleport`, irrevocably destroying Alice's `X` DOT worth of value on AssetHub (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:372-404`).
3. Concurrently/before the resulting XCM Transact executes on BridgeHub, an honest relayer submits the message with nonce `N` via `InboundQueue::submit`, causing `process_message` to run and mark the nonce consumed (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:215-245`).
4. The delayed XCM Transact `EthereumSystemCall::AddTip{sender: Alice, message_id: Inbound(N), amount: ether_gained}` executes on BridgeHub; `InboundQueue::add_tip` returns `Err(AddTipError::NonceConsumed)` because `Nonce::<T>::get(N)` is now true (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:248-258`).
5. `system-v2::add_tip` catches the error and only records `LostTips::<T>::mutate(&Alice, ...)` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:266-270`); it emits `TipProcessed{success: false}` and returns `Ok(())`.
6. Alice's `X` DOT (converted and burned as Ether) is permanently gone: it was never applied to any relayer's reward and there is no extrinsic anywhere in the codebase that reads `LostTips` to refund Alice.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-363)
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
