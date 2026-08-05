## Title
Foreign asset multi-reserve fungibility allows draining a bridge sovereign account via mismatched deposit/withdraw reserve routes - (File: `substrate/frame/assets/src/lib.rs`, `cumulus/parachains/runtimes/assets/common/src/matching.rs`)

### Summary
`pallet-assets` lets the owner of a foreign `AssetId` register **multiple independent trusted reserve locations** for that single asset via `set_reserves` [1](#0-0) . Once registered, all local balance units of that `AssetId` are treated as fully fungible regardless of which reserve chain actually backed them — the XCM reserve-matching logic (`TeleportableAssetWithTrustedReserve` / `NonTeleportableAssetFromTrustedReserve`) only checks whether a destination is *any* configured reserve for the asset, with no linkage to which reserve originally deposited the specific tokens held by a user [2](#0-1) . This mirrors the reported bug class exactly: a wrapped value is assumed 1:1 fungible across multiple backing sources, so an attacker can deposit through a cheap/weak source and withdraw through the strong source, draining that source's dedicated collateral.

### Finding Description
`pallet-assets::set_reserves` stores a bounded list (`MAX_RESERVES`) of `ForeignAssetReserveData { reserve, teleportable }` per `AssetId` [3](#0-2) . This is an intentional feature explicitly designed "for transferring assets with multiple configured reserves (such as Asset Hub ForeignAssets)" [4](#0-3) , i.e. combining the same logical token arriving from more than one bridge/chain under one fungible local `AssetId`.

The XCM executor's reserve-determination logic (`TeleportableAssetWithTrustedReserve` / `NonTeleportableAssetFromTrustedReserve`) only asks "is `b` *a* trusted reserve of asset `a`?" by membership-checking the `reserves` list — it never checks that the specific units a user is moving actually originated from that reserve [5](#0-4) . Balances in `pallet-assets` carry no per-unit provenance; once minted, a unit deposited via Reserve A is indistinguishable from a unit deposited via Reserve B.

This is the direct analog of the SolvBTC bug: two "assets" (here, two independent reserve chains/bridges backing the same `AssetId`) are treated as 1:1 fungible by the pooling contract/pallet, when in reality they represent separate collateral pools held in separate sovereign accounts. If Reserve A's collateral is compromised, depegged, or simply has a weaker security model (e.g. a newly-added or lower-liquidity bridge lane) while Reserve B holds significant real backing, a holder can:
1. Reserve-deposit (or teleport, if `teleportable=true`) tokens via the weak Reserve A, minting local `AssetId` balance.
2. Use `pallet_xcm::transfer_assets_using_type_and_then` with `TransferType::RemoteReserve(ReserveB)` (or `DestinationReserve`, if this chain is `dest`) to withdraw the same nominal amount from Reserve B's sovereign account [6](#0-5) .
3. Reserve B's sovereign account is debited even though it never received any deposit corresponding to that user's balance — draining collateral that was meant to back tokens deposited through Reserve B's own users.

### Impact Explanation
This breaks the "balances, assets... conserve value and settle exactly once to the rightful beneficiary and amount" invariant. It can silently drain the sovereign/collateral account of one bridge route using tokens whose real backing sits in a different, unrelated bridge/reserve account, causing permanent loss of bridged funds for that reserve chain's legitimate users — a direct fund-theft / fund-lock condition without needing any admin, validator, relayer, or governance compromise, only the (intended, documented) act of an asset owner configuring more than one reserve for a shared `AssetId`.

### Likelihood Explanation
Likelihood is architecture-dependent but non-trivial: it requires only that some `AssetId`'s owner (which is not necessarily a highly-trusted global admin — it is whichever account created/owns that specific foreign asset, frequently a parachain sovereign account) configures more than one reserve for the same asset, a pattern the codebase's own PR description recommends as a supported use case [4](#0-3) . Once two reserves exist for one `AssetId`, any ordinary signed user (no validator/relayer/governance access needed) can perform the deposit-cheap/withdraw-expensive arbitrage using standard `pallet-xcm` extrinsics.

### Recommendation
- Disallow configuring more than one reserve per `AssetId` unless the pallet also enforces a global cap that total redemptions per reserve cannot exceed that reserve's own tracked cumulative inflow (i.e., track balance provenance per reserve, not just a flat fungible total).
- Alternatively, require that `RemoteReserve`/`DestinationReserve` withdrawal specify and validate against the *same* reserve that most recently deposited-matching liquidity, or maintain per-reserve sub-balances instead of one flat fungible pool.
- Add a runtime invariant/test ensuring that the sum of amounts redeemable via each configured reserve never exceeds what was actually deposited through that specific reserve.

### Proof of Concept
1. Asset owner (e.g. sovereign account of Parachain X) creates foreign `AssetId` A on Asset Hub and calls `set_reserves(A, [ (ReserveChain1, teleportable=true), (ReserveChain2, teleportable=false) ])` via `pallet_assets::set_reserves` [7](#0-6) .
2. Attacker holds native tokens on ReserveChain1 (weak/newly-added bridge) and none on ReserveChain2 (established, well-funded bridge).
3. Attacker teleports/reserve-deposits N units of the underlying token from ReserveChain1 to Asset Hub, receiving N units of foreign `AssetId` A (fungible balance, no provenance tag).
4. Attacker calls `pallet_xcm::transfer_assets_using_type_and_then` specifying `TransferType::RemoteReserve(ReserveChain2)` for asset A with amount N, targeting some destination that trusts ReserveChain2 as reserve [6](#0-5) . The `IsReserve` filter accepts this because ReserveChain2 is simply present in asset A's `reserves` list [8](#0-7) .
5. ReserveChain2's sovereign account on Asset Hub is decremented by N, even though the attacker's N units never touched ReserveChain2 — draining real collateral backing ReserveChain2's own bridged users.

**Uncertainty note:** I could not fully verify, within this indexed subset of the repository, whether the currently deployed Asset Hub Westend/Rococo `ForeignAssetsReservesProvider` implementations ever configure more than one reserve for the same `AssetId` in production (the migration code I found assigns exactly one reserve per rule) [9](#0-8) . The vulnerability is a latent capability of the generic `pallet-assets` reserves feature and `set_reserves` extrinsic rather than a currently-exploited runtime configuration; whether it is live in any deployed chain depends on runtime-specific configuration not fully visible via the index. A Devin session with full repository/build access would be needed to confirm whether any production runtime currently sets `MAX_RESERVES > 1` for a given `AssetId`.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1938-1963)
```rust
		/// Sets the trusted reserve information of an asset.
		///
		/// Origin must be the Owner of the asset `id`. The origin must conform to the configured
		/// `CreateOrigin` or be the signed `owner` configured during asset creation.
		///
		/// - `id`: The identifier of the asset.
		/// - `reserves`: The full list of trusted reserves information.
		///
		/// Emits `AssetMinBalanceChanged` event when successful.
		#[pallet::call_index(33)]
		#[pallet::weight(T::WeightInfo::set_reserves(reserves.len() as u32))]
		pub fn set_reserves(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			reserves: BoundedVec<T::ReserveData, ConstU32<MAX_RESERVES>>,
		) -> DispatchResult {
			let id: T::AssetId = id.into();
			let origin = ensure_signed(origin.clone())
				.or_else(|_| T::CreateOrigin::ensure_origin(origin, &id))?;

			let details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
			ensure!(origin == details.owner, Error::<T, I>::NoPermission);

			Self::unchecked_update_reserves(id, reserves)?;
			Ok(())
		}
```

**File:** cumulus/parachains/runtimes/assets/common/src/matching.rs (L69-120)
```rust
/// Checks if asset `a` is coming from a trusted Reserve location `b`, then checks whether the local
/// chain is also a reserve of `a`. Assets can be teleported between their reserve locations.
pub struct TeleportableAssetWithTrustedReserve<SelfParaId, ReserveProvider, L = Location>(
	core::marker::PhantomData<(SelfParaId, ReserveProvider, L)>,
);
impl<
		SelfParaId: Get<ParaId>,
		L: TryFrom<Location> + TryInto<Location> + Clone + Debug,
		ReserveProvider: ProvideAssetReserves<Location, ForeignAssetReserveData>,
	> ContainsPair<L, L> for TeleportableAssetWithTrustedReserve<SelfParaId, ReserveProvider, L>
{
	fn contains(a: &L, b: &L) -> bool {
		tracing::trace!(target: "xcm::contains", ?a, ?b, "TeleportableAssetWithTrustedReserve");
		// We convert locations to latest
		let (a, b) = match ((*a).clone().try_into(), (*b).clone().try_into()) {
			(Ok(a), Ok(b)) => (a, b),
			_ => return false,
		};
		let reserves = ReserveProvider::reserves(&a);
		tracing::trace!(target: "xcm::contains", ?reserves, "TeleportableAssetWithTrustedReserve");
		// check if `b` is reserve for `a` and teleportable flag is set
		let filter = (b, true).into();
		reserves.contains(&filter)
	}
}

/// Checks if asset `a` is coming from a trusted Reserve location `b`.
/// Then checks that the local chain is NOT itself also reserve of `a`, otherwise a teleport is in
/// order.
pub struct NonTeleportableAssetFromTrustedReserve<SelfParaId, ReserveProvider, L = Location>(
	core::marker::PhantomData<(SelfParaId, ReserveProvider, L)>,
);
impl<
		SelfParaId: Get<ParaId>,
		L: TryFrom<Location> + TryInto<Location> + Clone + Debug,
		ReserveProvider: ProvideAssetReserves<Location, ForeignAssetReserveData>,
	> ContainsPair<L, L> for NonTeleportableAssetFromTrustedReserve<SelfParaId, ReserveProvider, L>
{
	fn contains(a: &L, b: &L) -> bool {
		tracing::trace!(target: "xcm::contains", ?a, ?b, "NonTeleportableAssetFromTrustedReserve");
		// We convert locations to latest
		let (a, b) = match ((*a).clone().try_into(), (*b).clone().try_into()) {
			(Ok(a), Ok(b)) => (a, b),
			_ => return false,
		};
		let reserves = ReserveProvider::reserves(&a);
		tracing::trace!(target: "xcm::contains", ?reserves, "NonTeleportableAssetFromTrustedReserve");
		// check if `b` is reserve for `a` and teleportable flag is NOT set
		let filter = (b, false).into();
		reserves.contains(&filter)
	}
}
```

**File:** substrate/frame/assets/src/functions.rs (L1146-1161)
```rust
	/// Helper function for setting reserves to be used in benchmarking and migrations.
	/// Does not check validity of asset id, caller should check it.
	pub fn unchecked_update_reserves(
		id: T::AssetId,
		reserves: BoundedVec<T::ReserveData, ConstU32<MAX_RESERVES>>,
	) -> Result<(), Error<T, I>> {
		if reserves.is_empty() {
			Reserves::<T, I>::remove(&id);
			Self::deposit_event(Event::ReservesRemoved { asset_id: id });
		} else {
			let reserves_vec = reserves.clone().into_inner();
			Reserves::<T, I>::set(&id, reserves);
			Self::deposit_event(Event::ReservesUpdated { asset_id: id, reserves: reserves_vec });
		}
		Ok(())
	}
```

**File:** prdoc/1.11.0/pr_3695.prdoc (L24-34)
```text
      By default, an asset's reserve is its origin chain. But sometimes we may want to
      explicitly use another chain as reserve (as long as allowed by runtime IsReserve
      filter).
      This is very helpful for transferring assets with multiple configured reserves
      (such as Asset Hub ForeignAssets), when the transfer strictly depends on the used
      reserve location.

      E.g. For transferring a bridged Foreign Assets between local parachains, Asset Hub
      or the parachain that bridged the asset over must be used as the reserve location.
      Same when transferring bridged assets back across the bridge, the local bridging
      parachain must be used as the explicit reserve location.
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1575-1604)
```rust
		/// Transfer assets from the local chain to the destination chain using explicit transfer
		/// types for assets and fees.
		///
		/// `assets` must have same reserve location or may be teleportable to `dest`. Caller must
		/// provide the `assets_transfer_type` to be used for `assets`:
		///  - `TransferType::LocalReserve`: transfer assets to sovereign account of destination
		///    chain and forward a notification XCM to `dest` to mint and deposit reserve-based
		///    assets to `beneficiary`.
		///  - `TransferType::DestinationReserve`: burn local assets and forward a notification to
		///    `dest` chain to withdraw the reserve assets from this chain's sovereign account and
		///    deposit them to `beneficiary`.
		///  - `TransferType::RemoteReserve(reserve)`: burn local assets, forward XCM to `reserve`
		///    chain to move reserves from this chain's SA to `dest` chain's SA, and forward another
		///    XCM to `dest` to mint and deposit reserve-based assets to `beneficiary`. Typically
		///    the remote `reserve` is Asset Hub.
		///  - `TransferType::Teleport`: burn local assets and forward XCM to `dest` chain to
		///    mint/teleport assets and deposit them to `beneficiary`.
		///
		/// On the destination chain, as well as any intermediary hops, `BuyExecution` is used to
		/// buy execution using transferred `assets` identified by `remote_fees_id`.
		/// Make sure enough of the specified `remote_fees_id` asset is included in the given list
		/// of `assets`. `remote_fees_id` should be enough to pay for `weight_limit`. If more weight
		/// is needed than `weight_limit`, then the operation will fail and the sent assets may be
		/// at risk.
		///
		/// `remote_fees_id` may use different transfer type than rest of `assets` and can be
		/// specified through `fees_transfer_type`.
		///
		/// The caller needs to specify what should happen to the transferred assets once they reach
		/// the `dest` chain. This is done through the `custom_xcm_on_dest` parameter, which
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/migrations.rs (L40-69)
```rust
pub struct AssetHubWestendForeignAssetsReservesProvider;
impl ForeignAssetsReservesProvider for AssetHubWestendForeignAssetsReservesProvider {
	type ReserveData = ForeignAssetReserveData;
	fn reserves_for(asset_id: &Location) -> Vec<Self::ReserveData> {
		let reserves = if StartsWith::<RococoEcosystem>::contains(asset_id) {
			// rule 3: rococo asset, Asset Hub Rococo reserve, non teleportable
			vec![(AssetHubRococo::get(), false).into()]
		} else if StartsWith::<EthereumLocation>::contains(asset_id) {
			// rule 2: ethereum asset, ethereum reserve, non teleportable
			vec![(EthereumLocation::get(), false).into()]
		} else {
			match asset_id.unpack() {
				(1, interior) => {
					match interior.first() {
						Some(Junction::Parachain(sibling_para_id))
							if sibling_para_id.ne(&ASSET_HUB_ID) =>
						{
							// rule 1: sibling parachain asset, sibling parachain reserve,
							// teleportable
							vec![ForeignAssetReserveData {
								reserve: Location::new(1, Junction::Parachain(*sibling_para_id)),
								teleportable: true,
							}]
						},
						_ => vec![],
					}
				},
				_ => vec![],
			}
		};
```
