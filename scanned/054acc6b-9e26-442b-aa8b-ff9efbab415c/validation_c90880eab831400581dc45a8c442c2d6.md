This confirms the critical design flaw: `add_tip` on BridgeHub (`bridges/snowbridge/pallets/system-v2/src/lib.rs`) explicitly acknowledges that tips can be "lost" (`LostTips` storage, `TipProcessed{success: false}` event) — but this only handles the failure *on BridgeHub itself*. It does not, and cannot, handle the case where the AssetHub-side burn already occurred but the cross-chain `Transact` message never executes or is dropped before even reaching this logic.

### Title
Irrecoverable loss of user funds when unprivileged `register_token`/`add_tip` burns fee assets on AssetHub before an unverified, unpaid cross-chain `Transact` to BridgeHub - (File: bridges/snowbridge/pallets/system-frontend/src/lib.rs)

### Summary
`snowbridge-pallet-system-frontend`'s public extrinsics `register_token` and `add_tip` (callable by any signed account satisfying only a loose origin/location check) irreversibly swap-and-burn the caller's fee/tip asset on AssetHub *before* sending an `UnpaidExecution`/`Transact` XCM to BridgeHub that actually performs the corresponding action (`RegisterToken`/`AddTip`) and only there decides success or failure. Because AssetHub's burn and BridgeHub's remote dispatch are two separate, non-atomic chains of execution connected only by a best-effort, unpaid XCM message, any failure, filtering, or origin mismatch on the BridgeHub side leaves the user's asset permanently burned with no message, no refund, and no recorded claim.

### Finding Description
`register_token`/`add_tip` are public, unprivileged extrinsics on AssetHub, gated only by `RegisterTokenOrigin`/a plain `ensure_signed`: [1](#0-0) [2](#0-1) 

Both paths call `swap_fee_asset_and_burn`, which swaps the supplied asset for Ether and then calls `burn_for_teleport`, destroying the local balance unconditionally and irreversibly, *before* the cross-chain message is even constructed: [3](#0-2) 

The resulting `Transact` call is wrapped with `UnpaidExecution { weight_limit: Unlimited, check_origin: None }` and dispatched via `send_transact_call`/`send_xcm`: [4](#0-3) [5](#0-4) 

On BridgeHub, `snowbridge-pallet-system-v2::register_token`/`add_tip` independently re-validates the origin via `T::FrontendOrigin::ensure_origin` and then attempts the actual action: [6](#0-5) [7](#0-6) 

Notably, the pallet's own authors recognize the failure mode for tips — they store lost tips in `LostTips` and emit `TipProcessed{success:false}` — but this only covers failures *internal to BridgeHub processing* (e.g. nonce already consumed): [8](#0-7) [7](#0-6) 

Because the AssetHub burn and the BridgeHub dispatch are two entirely separate, non-atomic Substrate transactions joined only by best-effort XCM transport (no reserve/escrow, no delivery receipt, `check_origin: None` so the executor doesn't even enforce the descending origin matches the expected sender), any of the following non-malicious, non-privileged conditions permanently destroys the user's funds with zero recovery path:
- The XCM message is dropped, fails to decode, or the `MessageQueue` on BridgeHub reports `Processed{success: false}` (analogous to the `exploit_v2_route_with_legacy_v1_transfer_will_fail` failure path already observed elsewhere in this codebase for a different message type).
- `T::FrontendOrigin::ensure_origin` on BridgeHub rejects the descended origin (e.g., mismatch between AssetHub's `PalletLocation`/reanchoring and BridgeHub's configured `FrontendOrigin` filter).
- The Ethereum-bound `OutboundQueue::validate`/`deliver` call inside `system-v2::send` fails (e.g., `Error::<T>::Send`), which propagates back as a failed extrinsic on BridgeHub — the burn on AssetHub has already happened in a prior, separate extrinsic and cannot be rolled back.

This directly mirrors the external report's broken invariant: an unprivileged actor triggers an irreversible on-chain state change (asset burn) that is coupled to a cross-chain message whose delivery/execution is not guaranteed and not atomically tied to that state change, resulting in fund loss with no legitimate way to prevent or reverse it — exactly the "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.

### Impact Explanation
Any user calling `register_token` or `add_tip` risks permanent, unrecoverable loss of the swapped/burned fee or tip asset if the paired BridgeHub-side action fails for any reason outside the user's control (origin misconfiguration, decode/dispatch failure, weight exhaustion, halted export mode changing mid-flight, OutboundQueue rejection). This is systemic fund loss risk affecting ordinary users of Snowbridge's token registration and tip mechanism on BridgeHub, not merely a griefing vector — it satisfies the "permanent user-fund or bridge-state lock" / "theft or unbacked... loss" impact category.

### Likelihood Explanation
No malicious peer, validator, relayer, or governance actor is required — this is triggerable by any ordinary signed account (for `add_tip`) or any account satisfying `RegisterTokenOrigin` (for `register_token`) under entirely normal operating conditions; XCM message failures, decode mismatches, and cross-chain configuration drift are realistic, expected occurrences in production (as evidenced by the pallet already needing a `LostTips` bookkeeping mechanism for the narrower, BridgeHub-internal failure case).

### Recommendation
Do not burn/destroy the fee or tip asset until successful execution and settlement is confirmed on BridgeHub. Prefer an escrow/hold pattern (reserve, don't burn) on AssetHub combined with a settlement-confirmation callback (e.g., a `QueryResponse`/receipt-based flow) that only finalizes the burn after the BridgeHub-side `RegisterToken`/`AddTip` command has been durably queued (or better, after `OutboundQueue::deliver` succeeds), with an explicit refund path back to the sender if the remote step fails, mirroring the existing `LostTips` idea but extended to cover cross-chain delivery/dispatch failure, not just BridgeHub-internal processing failure.

### Proof of Concept
1. Caller invokes `EthereumSystemFrontend::add_tip(origin=signed(Alice), message_id, asset)` on AssetHub with a valid tip asset.
2. `swap_fee_asset_and_burn` executes `swap_and_burn`, permanently destroying Alice's asset via `burn_for_teleport` in this same, already-committed extrinsic — see [9](#0-8) .
3. `send_transact_call` dispatches an `UnpaidExecution`+`Transact` XCM to BridgeHub with `check_origin: None` — see [4](#0-3) .
4. On BridgeHub, suppose the referenced `message_id`'s nonce was already consumed (a routine, non-malicious race condition — the pallet itself anticipates this): `OutboundQueue::add_tip`/`InboundQueue::add_tip` returns `Err`, `LostTips` is incremented, and `TipProcessed{success:false}` is emitted — see [10](#0-9) .
5. Alice's Ether is gone from AssetHub (burned in step 2) with no compensating credit anywhere; `LostTips` on BridgeHub records the loss but provides "no recovery method" per its own doc comment (`bridges/snowbridge/pallets/system-v2/src/lib.rs:136-139`), confirming the fund loss is final by design in the current implementation.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L290-317)
```rust
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L406-423)
```rust
		fn send_transact_call(
			origin_location: Location,
			call: BridgeHubRuntime<T>,
		) -> DispatchResult {
			let dest = T::BridgeHubLocation::get();
			let remote_xcm = Self::build_remote_xcm(&call);
			let message_id = Self::send_xcm(origin_location, dest.clone(), remote_xcm.clone())
				.map_err(|error| Error::<T>::from(error))?;

			Self::deposit_event(Event::<T>::MessageSent {
				origin: T::PalletLocation::get().into(),
				destination: dest,
				message: remote_xcm,
				message_id,
			});

			Ok(())
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L209-249)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::register_token())]
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
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
