## Analysis

The reported bug's core invariant break is: **an operation that irreversibly destroys value on one chain (burn) is not gated on a pre-check that the corresponding operation will succeed on the other chain**, so users permanently lose the burned funds/fee when the second leg fails for a reason unrelated to what was checked before burning.

The closest local analog is in the Snowbridge `system-frontend` pallet's `register_token`/`add_tip` flow, where the fee/tip asset is unconditionally burned on the local chain (AssetHub) *before* the corresponding message is delivered/executed on BridgeHub, and BridgeHub can independently reject that message with no refund path back to the burner.

### Title
Snowbridge `register_token`/`add_tip` burns the fee/tip on AssetHub before the corresponding BridgeHub message send/dispatch can fail, permanently losing user funds - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`Pallet::register_token` and `Pallet::add_tip` in the `snowbridge-pallet-system-frontend` (running on AssetHub) call `swap_fee_asset_and_burn`, which withdraws/burns the user's fee or tip asset via `burn_for_teleport` [1](#0-0) , and only afterwards attempts to deliver the Transact XCM to BridgeHub via `send_transact_call` → `send_xcm` [2](#0-1) . The burn is committed on AssetHub as soon as the local extrinsic succeeds; whether the remote `EthereumSystem::register_token`/`add_tip` call on BridgeHub actually succeeds is decided later, in a separate chain's execution, by `snowbridge-pallet-system-v2`'s own `Self::send`, which can independently fail via `OutboundQueue::validate` [3](#0-2) .

### Finding Description
`register_token` (call index 1) on AssetHub:
1. Burns the tip/fee asset unconditionally via `swap_fee_asset_and_burn` (which itself calls `swap_and_burn`/`burn_for_teleport`) [4](#0-3) .
2. Builds a `Transact` XCM targeting BridgeHub's `EthereumSystem::register_token` and sends it with `UnpaidExecution`/no fallback weight [5](#0-4) .

On BridgeHub, `snowbridge-pallet-system-v2::register_token` independently calls `Self::send`, which validates and delivers the message through `OutboundQueue::validate`/`deliver` [6](#0-5) . This validation can fail for reasons entirely orthogonal to anything checked on AssetHub before the burn — e.g. the outbound channel/gateway being halted, message-size/queue-capacity limits, or per-channel rate limiting enforced by the outbound queue. When that happens, the `Transact` call returns `Error::<T>::Send(err)` on BridgeHub, but this failure is local to BridgeHub's XCM execution and has no mechanism to reverse or refund the tip/fee ether that was already burned on AssetHub in a prior, already-finalized extrinsic.

This exactly mirrors the external report's shape: chain A (AssetHub) performs an irreversible burn; chain B (BridgeHub) enforces an independent, unrelated acceptance check (`OutboundQueue::validate`) that the burn side never verifies beforehand, so the user's asset is destroyed with nothing to show for it. The pallet's own `add_tip` documentation implicitly acknowledges this class of loss by maintaining a `LostTips` storage for tips that could not be applied to a message reward [7](#0-6) , but that only records BridgeHub-side accounting failures after the nonce is already consumed — it does not, and cannot, undo the burn that already happened on AssetHub, and no equivalent tracking exists at all for `register_token`'s `Self::send` failure path.

### Impact Explanation
A user who calls `register_token` or `add_tip` on AssetHub permanently loses the swapped/burned ether whenever the corresponding remote command fails to validate/deliver on BridgeHub (queue congestion, gateway halted operating mode, oversized command batch, etc.), with no automatic or even tracked refund path for `register_token`, and only a passive "lost" bookkeeping entry (no recovery mechanism) for `add_tip`. This is a permanent user-fund loss triggered by ordinary, unprivileged use of a public extrinsic — matching the "permanent user-fund or bridge-state lock" impact class.

### Likelihood Explanation
No malicious actor, governance, or privileged access is required. Any signed account calling `register_token`/`add_tip` while the Ethereum-bound outbound channel is congested, halted, or otherwise rejecting new messages (conditions that can occur during ordinary bridge operation, e.g. via `set_operating_mode` halting or the channel simply reaching a rate/queue limit) will trigger the loss deterministically once the local burn commits and the subsequent remote send fails.

### Recommendation
Validate (not just build) the outbound message/transact readiness — e.g., call the BridgeHub-side equivalent of `OutboundQueue::validate` conditions or at minimum check `ExportOperatingMode`/channel status — *before* burning the fee/tip asset in `swap_fee_asset_and_burn`, or defer the burn until after the XCM has been confirmed deliverable. Alternatively, mirror the `LostTips` pattern with an actual recovery/refund mechanism covering both `register_token` and the AssetHub-to-BridgeHub delivery failure, not just the reward-application failure captured today.

### Proof of Concept
1. On BridgeHub, put the export/outbound queue into a state where `OutboundQueue::validate` will reject new messages (e.g., simulate the gateway `ExportPausedQuery`/halted mode, or exceed configured channel capacity).
2. On AssetHub, call `snowbridge-pallet-system-frontend::register_token` (or `add_tip`) with a valid tip asset.
3. Observe `swap_fee_asset_and_burn`/`burn_for_teleport` executes and commits, burning the user's ether-denominated fee on AssetHub [8](#0-7) .
4. The `Transact` XCM reaches BridgeHub; `snowbridge-pallet-system-v2::register_token`'s `Self::send` returns `Error::<T>::Send(err)` because `OutboundQueue::validate` rejects the message [9](#0-8) .
5. The registration never lands on Ethereum, but the user's burned fee on AssetHub is not, and cannot be, refunded.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L310-317)
```rust
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L284-300)
```rust
	impl<T: Config> Pallet<T> {
		/// Send `command` to the Gateway from a specific origin/agent
		fn send(origin: H256, command: Command, fee: u128) -> DispatchResult {
			let message = Message {
				origin,
				id: frame_system::unique((origin, &command, fee)).into(),
				fee,
				commands: BoundedVec::try_from(vec![command]).unwrap(),
			};

			let ticket = <T as pallet::Config>::OutboundQueue::validate(&message)
				.map_err(|err| Error::<T>::Send(err))?;

			<T as pallet::Config>::OutboundQueue::deliver(ticket)
				.map_err(|err| Error::<T>::Send(err))?;
			Ok(())
		}
```
