### Title
Coretime Region reserve-transfer can strand a Region owner-less on the sending chain if the outbound XCM program aborts after `BurnAsset` but before delivery — permanently unrecoverable by anyone except governance - (File: `substrate/frame/broker/src/nonfungible_impl.rs`)

### Summary
`pallet-broker`'s `Mutate`/`Transfer` implementation models an XCM reserve-transfer of a coretime Region not by real burn/mint, but by flipping the `owner` field of the `RegionRecord` between `Some(account)` and `None`. `burn` (triggered by `WithdrawAsset`/reserve-withdraw legs of an XCM program) sets `owner = None`; `mint_into` (triggered by `deposit_asset` on the receiving side) requires `owner.is_none()` and sets it back to `Some(who)`. This mirrors the Balancer bug class exactly: one code path only performs half of a two-step "lock/unlock" state transition, and the counterpart step is gated on the exact companion state produced by the first step. Once a Region is burned (`owner = None`) on the source chain but the corresponding mint never executes on the destination (message drop, XCM decode/dispatch failure downstream, `DepositAsset` filter mismatch, weight/fee failure, or any of the many non-atomic hops in `remote_reserve_transfer_program`/`destination_reserve_transfer_programs`), the Region record persists on-chain with `owner: None` forever. No signed extrinsic can set the owner back — `transfer`, `partition`, `interlace`, `assign` all require `Some(current_owner) == check_owner`, which can never match `None`. The only recovery path is `force_transfer`, restricted to `T::AdminOrigin`/root — i.e., governance intervention, not a self-service extrinsic.

### Finding Description
`pallet-broker` implements the `nonfungible::Mutate`/`Transfer` traits used by XCM asset transactors so that Regions (coretime NFTs) can move across chains via reserve-transfer: [1](#0-0) 

- `burn(item, maybe_check_owner)` sets `record.owner = None` and re-inserts the record — this is the on-chain effect of `WithdrawAsset`/`InitiateReserveWithdraw` executing on the source chain.
- `mint_into(item, who)` requires `record.owner.is_none()` (i.e. it must have been "burned" first) and then calls `Self::issue(..., Some(who.clone()), ...)` to set the new owner — this is the effect of `DepositAsset`/`ReserveAssetDeposited` executing on the destination chain.

These two steps are executed as **separate XCM instructions, in separate legs of a cross-chain program, potentially on separate chains and in separate blocks**, per the reserve-transfer builders in `pallet-xcm`: [2](#0-1) 

The local leg (`WithdrawAsset` + `BurnAsset`) executes and commits on the source chain, unconditionally advancing the Region's on-chain state to `owner: None`, *before* the remote leg (`WithdrawAsset` + `DepositAsset` on the destination) is even attempted. If the outbound message is never delivered, is dropped by the transport layer, fails to decode/dispatch on the destination, or the destination's `DepositAsset`/asset-transactor rejects the Region (e.g. destination doesn't recognize the `BrokerPalletLocation` asset, weight limit too low, filter mismatch), the mint step (`mint_into`) never runs. The Region is left in the `owner: None` state indefinitely.

Compare this to the reported bug's structural shape:
- Balancer: `initialize` (one-time, done via any router) is the state-advancing step; a *different, mandatory* companion state (NFT mint gating withdrawal) is only produced by the specific hook-router's `addLiquidityProportional`/`onAfterAddLiquidity`. If initialization happens through a plain router, the NFT never gets minted, and `onAfterRemoveLiquidity`'s NFT-burn check makes withdrawal permanently impossible.
- Broker: `burn` (state-advancing, happens synchronously and irreversibly on the source chain) is decoupled from `mint_into` (the mandatory companion step that is required to ever regain a usable owner). If the cross-chain leg that performs the mint fails for any of the many independent reasons XCM delivery/execution can fail, the companion step never runs, and the Region is stuck with `owner: None` — an unrecoverable state via any user-facing extrinsic.

None of the local `pallet-broker` extrinsics can undo this because every ownership-mutating call requires matching `Some(current_owner)`: [3](#0-2) 

The only escape hatch is governance: [4](#0-3) 

which explicitly documents this exact scenario ("recover regions that have been 'burned' (e.g., from an XCM reserve transfer)") — i.e. the pallet authors are aware the burn/mint split can strand a Region, and the only remedy is a privileged root/AdminOrigin call, not something the Region owner can trigger themselves.

### Impact Explanation
A Region represents purchased/valuable bulk coretime (potentially large sums, since `sale_price` can be substantial and Regions can also carry auto-renewal rights). Any user-unprivileged-triggerable path (an ordinary account submitting a reserve-transfer XCM through `pallet-xcm`'s public `transfer_assets`/`limited_reserve_transfer_assets` extrinsics with a Region asset and a destination that fails to complete the deposit) can drive the Region into an `owner: None` state on the source chain that is **not recoverable by the account that owned it** — funds/rights are permanently locked absent a privileged governance call. This matches the "permanent user-fund or bridge-state lock" impact category in the gate criteria; it does not require a malicious peer/validator/relayer — an entirely honest but unlucky (or destination-misconfigured) cross-chain transfer suffices.

### Likelihood Explanation
Triggering the burn step requires only a normal signed reserve-transfer extrinsic naming a Region asset and a destination; the local leg (`WithdrawAsset`+`BurnAsset`) is unconditional and irreversible once it commits. Causing the remote leg to fail is not attacker-exotic: insufficient weight/fee limit, an unconfigured/incompatible `IsReserve`/asset-transactor on the destination chain, message queue congestion, or simply targeting a chain/version that doesn't yet support Region deposit (a very plausible real-world condition given Region cross-chain support is a newer feature per `pr_3455.prdoc`) are all realistic, non-adversarial failure modes. The fact that `force_transfer`'s own doc comment calls out recovering "regions that have been burned (e.g., from an XCM reserve transfer)" is direct evidence that this failure mode is expected to occur in practice.

### Recommendation
- Make the source-chain burn conditional on successful remote delivery/execution acknowledgment, or otherwise make the reserve-transfer atomic across the full round-trip (e.g., only commit `owner = None` after confirming delivery, or hold the Region in an intermediate "in-transit" state that a permissionless "reclaim on failure/timeout" extrinsic can revert back to the original owner if remote confirmation doesn't arrive within a bounded window).
- Alternatively/additionally, add a permissionless, non-governance recovery extrinsic that lets the *last known owner* (tracked before burn, distinct from the nulled `owner` field) reclaim a Region that has sat in `owner: None` for longer than a defined timeout, removing the reliance on `AdminOrigin`/root for what is fundamentally a user self-service action.
- Ensure the destination-side deposit/mint step is retried or explicitly surfaced as a failure event that off-chain tooling can act on, rather than silently leaving the Region stranded.

### Proof of Concept
1. On the Coretime chain, account `A` purchases a Region via `Broker::purchase`, becoming `owner: Some(A)` (`RegionRecord.owner`).
2. `A` submits `pallet_xcm::transfer_assets` (or `limited_reserve_transfer_assets`) specifying the Region (`NonFungible(Index(region_id))`, `AssetId(BrokerPalletLocation)`) with destination `D` (e.g., a parachain that has not registered/handles the Region asset in its `DepositAsset` filter, or with an insufficient `weight_limit`).
3. Locally, `destination_reserve_transfer_programs` builds and executes `WithdrawAsset` + `BurnAsset` on the Coretime chain — this calls `NonFungible::burn(&region_id, Some(&A))`, which unconditionally sets `RegionRecord.owner = None` (`nonfungible_impl.rs:90-101`) and commits.
4. The onward XCM (`WithdrawAsset` + `ClearOrigin` + `DepositAsset`) is sent to `D`; `D` fails to execute the `DepositAsset` step (unsupported asset / insufficient weight / filter reject) — the message errors out or is silently dropped, and `mint_into` never runs on `D`, and no compensating instruction returns the Region to `A` on the Coretime chain.
5. Back on the Coretime chain, `Regions::<T>::get(region_id).owner == None` permanently. `A` (or anyone) calling `Broker::transfer`, `Broker::partition`, `Broker::interlace`, or `Broker::assign` on `region_id` fails with `Error::<T>::NotOwner`/ownership checks (`check_ownership_for_transfer_or_partition_or_interlace` test pattern, `tests.rs:1908-1921`), since these require `Some(owner) == check_owner` and owner is `None`.
6. The Region, and the coretime value it represents, is unrecoverable by `A`; only `Broker::force_transfer` under `T::AdminOrigin`/root can restore it (`lib.rs:1067-1083`), confirming this is a real, pallet-author-acknowledged stranding scenario reachable via ordinary, non-malicious XCM usage.

### Citations

**File:** substrate/frame/broker/src/nonfungible_impl.rs (L55-101)
```rust
/// We don't really support burning and minting.
///
/// We only need this to allow the region to be reserve transferable.
///
/// For reserve transfers that are not 'local', the asset must first be withdrawn to the holding
/// register and then deposited into the designated account. This process necessitates that the
/// asset is capable of being 'burned' and 'minted'.
///
/// Since each region is associated with specific record data, we will not actually burn the asset.
/// If we did, we wouldn't know what record to assign to the newly minted region. Therefore, instead
/// of burning, we set the asset's owner to `None`. In essence, 'burning' a region involves setting
/// its owner to `None`, whereas 'minting' the region assigns its owner to an actual account. This
/// way we never lose track of the associated record data.
impl<T: Config> Mutate<T::AccountId> for Pallet<T> {
	/// Deposit a region into an account.
	fn mint_into(item: &Self::ItemId, who: &T::AccountId) -> DispatchResult {
		let region_id: RegionId = (*item).into();
		let record = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;

		// 'Minting' can only occur if the asset has previously been burned (i.e. moved to the
		// holding register)
		ensure!(record.owner.is_none(), Error::<T>::NotAllowed);
		Self::issue(
			region_id.core,
			region_id.begin,
			region_id.mask,
			record.end,
			Some(who.clone()),
			record.paid,
		);

		Ok(())
	}

	/// Withdraw a region from account.
	fn burn(item: &Self::ItemId, maybe_check_owner: Option<&T::AccountId>) -> DispatchResult {
		let region_id: RegionId = (*item).into();
		let mut record = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;
		if let Some(owner) = maybe_check_owner {
			ensure!(Some(owner.clone()) == record.owner, Error::<T>::NotOwner);
		}

		record.owner = None;
		Regions::<T>::insert(region_id, record);

		Ok(())
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2550-2578)
```rust
		// XCM instructions to be executed on local chain
		let mut local_execute_xcm = Xcm(vec![
			// withdraw reserve-based assets
			WithdrawAsset(assets.clone()),
			// burn reserve-based assets
			BurnAsset(assets),
		]);
		// XCM instructions to be executed on destination chain
		let mut xcm_on_dest = Xcm(vec![
			// withdraw `assets` from origin chain's sovereign account
			WithdrawAsset(reanchored_assets),
			// following instructions are not exec'ed on behalf of origin chain anymore
			ClearOrigin,
		]);
		// handle fees
		Self::add_fees_to_xcm(dest, fees, weight_limit, &mut local_execute_xcm, &mut xcm_on_dest)?;

		// Use custom XCM on remote chain, or just default to depositing everything to beneficiary.
		let custom_remote_xcm = match beneficiary {
			Either::Right(custom_xcm) => custom_xcm,
			Either::Left(beneficiary) => {
				// deposit all remaining assets in holding to `beneficiary` location
				Xcm(vec![DepositAsset { assets: Wild(AllCounted(max_assets)), beneficiary }])
			},
		};
		xcm_on_dest.0.extend(custom_remote_xcm.into_iter());

		Ok((local_execute_xcm, xcm_on_dest))
	}
```

**File:** substrate/frame/broker/src/tests.rs (L1908-1921)
```rust
#[test]
fn check_ownership_for_transfer_or_partition_or_interlace() {
	TestExt::new().endow(1, 1000).execute_with(|| {
		assert_ok!(Broker::do_start_sales(100, 1));
		advance_to(2);
		let region = Broker::do_purchase(1, u64::max_value()).unwrap();
		assert_noop!(Broker::do_transfer(region, Some(2), 2), Error::<Test>::NotOwner);
		assert_noop!(Broker::do_partition(region, Some(2), 2), Error::<Test>::NotOwner);
		assert_noop!(
			Broker::do_interlace(region, Some(2), CoreMask::from_chunk(0, 20)),
			Error::<Test>::NotOwner
		);
	});
}
```

**File:** substrate/frame/broker/src/lib.rs (L1067-1083)
```rust
		///
		/// This can also be used to recover regions that have been "burned" (e.g., from an
		/// XCM reserve transfer).
		///
		/// - `origin`: Must be Root or pass `AdminOrigin`.
		/// - `region_id`: The Region whose ownership should change.
		/// - `new_owner`: The new owner for the Region.
		#[pallet::call_index(28)]
		pub fn force_transfer(
			origin: OriginFor<T>,
			region_id: RegionId,
			new_owner: T::AccountId,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin_or_root(origin)?;
			Self::do_transfer(region_id, None, new_owner)?;
			Ok(())
		}
```
