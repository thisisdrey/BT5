## Title
Same `weight_limit` reused for both reserve-chain and destination-chain `BuyExecution` in remote-reserve transfers, though `fees` are amount-split unevenly - (File: `polkadot/xcm/pallet-xcm/src/lib.rs`)

### Summary
The Connext bug reused a single `slippageTol` parameter to protect two economically distinct swaps (source-chain and destination-chain), even though the correct tolerance differs per swap, causing one leg to be effectively unprotected. `pallet-xcm`'s `remote_reserve_transfer_program` has the same structural flaw: a single caller-supplied `weight_limit` is applied unmodified to *both* the `BuyExecution` on the intermediate `reserve` chain and the `BuyExecution` on the final `dest` chain, while the `fees` asset backing those two executions is *not* shared equally in a way that matches real per-hop weight needs — it is naively halved by `halve_fees`, with no way for the caller to size each leg independently.

### Finding Description
`transfer_assets`/`limited_reserve_transfer_assets` accept one `weight_limit: WeightLimit` parameter from the caller [1](#0-0) . When the transfer type resolves to `RemoteReserve`, this same `weight_limit` value is threaded into `remote_reserve_transfer_program`, which builds two separate XCM programs to run on two separate, independently-metered chains — the `reserve` chain and the final `dest` chain: [2](#0-1) 

The fee asset is naively split in half (`halve_fees`), and then the *same* `weight_limit` value is applied to `BuyExecution` on the reserve chain and to `BuyExecution` on the destination chain: [3](#0-2) [4](#0-3) 

This mirrors the Connext bug precisely: two conceptually different executions (weight cost on the reserve chain doing `InitiateReserveWithdraw`/`DepositReserveAsset` vs. weight cost on the destination chain doing final `DepositAsset`) are protected by one shared bound, while the backing budget (`fees`) is split evenly regardless of actual need on each leg. If the two chains have different execution costs (very plausible since the reserve chain and destination chain run different XCM programs of different complexity), a `weight_limit` that is adequate for one leg can be wildly mismatched for the other:
- If `weight_limit` is a `Definite` value sized for the heavier leg, the lighter leg may vastly overpay or the `BuyExecution` may simply succeed leaving no bound-related protection at all on that leg.
- If `weight_limit` is sized for the lighter leg, the heavier leg's `BuyExecution` can fail (`weight_limit` too low relative to actual weight, causing insufficient fees to be purchased), causing the message to trap assets mid-route with no ability for the user to specify different tolerances for each hop — the assets get stuck in the reserve chain's holding/trapped-assets state, analogous to Connext's "transfers get stuck during periods of instability."

Unlike `swap_exact_tokens_for_tokens` in `pallet-asset-conversion`, where `amount_out_min` legitimately protects a single atomic multi-hop swap (all hops execute or roll back together in one transaction), the two `BuyExecution`s here execute on two *different, asynchronous chains* — exactly the source/destination split the Connext report is about — so a single shared parameter cannot correctly express two independent tolerances.

### Impact Explanation
This affects `pallet-xcm`'s public, unprivileged `transfer_assets`/`limited_reserve_transfer_assets` extrinsics, used heavily by Snowbridge/Asset Hub-style remote-reserve routes. Mis-binding of a single weight bound across two independently metered chains can cause:
- Assets/fees becoming trapped on the reserve or destination chain when the shared `weight_limit` under-provisions one leg, requiring `claim_assets` recovery (permanent lock until a manual, non-atomic claim, if even possible) — matching "permanent user-fund or bridge-state lock."
- Public underpriced work: an attacker (any caller) can choose a `weight_limit`/fee split that starves one leg's `BuyExecution`, causing failed/incomplete remote execution that still consumes reserve-chain resources without corresponding fee payment on the destination leg.

### Likelihood Explanation
Any unprivileged, signed account can call `transfer_assets` with `TransferType::RemoteReserve`, which is a normal, documented usage path (not requiring governance, relayer, or validator misbehavior). The mismatch is triggered purely by economics of the two remote chains' respective weight costs versus the even 50/50 fee split, which is a realistic, everyday condition (different parachains have different accounting/weight costs), not a contrived edge case.

### Recommendation
Allow separate weight limits (and a configurable, non-50/50 fee split) for the reserve-chain leg and the destination-chain leg of a `RemoteReserve` transfer, instead of reusing a single `weight_limit` value across both `BuyExecution` instructions in `remote_reserve_transfer_program`.

### Proof of Concept
Conceptual PoC (cannot be executed without live network parameters, since exact weight costs are chain-specific runtime data):
1. Call `pallet_xcm::transfer_assets` with `TransferType::RemoteReserve(reserve)`, a `fees` asset, and a `weight_limit::Limited(w)` chosen such that `w` is sufficient to cover the reserve chain's `BuyExecution`+`InitiateReserveWithdraw`+`DepositReserveAsset` cost but, after `halve_fees` splits `fees` in half, `fees_half_2` (destination leg) is insufficient to purchase `w` worth of weight on `dest` (whose per-unit weight price differs from `reserve`'s).
2. Observe: reserve leg's `BuyExecution` succeeds (weight sufficiently funded), message forwarded to `dest`.
3. On `dest`, `BuyExecution { fees: dest_fees, weight_limit: w }` fails because `dest_fees` cannot buy `w` weight at `dest`'s price — the deposited assets and fees are trapped in `dest`'s XCM holding register, requiring the recipient to later call `claim_assets` (if trap location/version is known) to recover funds, exactly the "user transfers get stuck" outcome described in the Connext report. [5](#0-4)

### Citations

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1456-1463)
```rust
		pub fn transfer_assets(
			origin: OriginFor<T>,
			dest: Box<VersionedLocation>,
			beneficiary: Box<VersionedLocation>,
			assets: Box<VersionedAssets>,
			fee_asset_item: u32,
			weight_limit: WeightLimit,
		) -> DispatchResult {
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2581-2589)
```rust
	fn remote_reserve_transfer_program(
		origin: Location,
		reserve: Location,
		beneficiary: Either<Location, Xcm<()>>,
		dest: Location,
		assets: Vec<Asset>,
		fees: Asset,
		weight_limit: WeightLimit,
	) -> Result<Xcm<<T as Config>::RuntimeCall>, Error<T>> {
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2596-2634)
```rust
		// we spend up to half of fees for execution on reserve and other half for execution on
		// destination
		let (fees_half_1, fees_half_2) = Self::halve_fees(fees)?;
		// identifies fee item as seen by `reserve` - to be used at reserve chain
		let reserve_fees = fees_half_1
			.reanchored(&reserve, &context)
			.map_err(|e| {
				tracing::error!(target: "xcm::pallet_xcm::remote_reserve_transfer_program", ?e, ?reserve, ?context, "Failed to re-anchor reserve_fees");
				Error::<T>::CannotReanchor
			})?;
		// identifies fee item as seen by `dest` - to be used at destination chain
		let dest_fees = fees_half_2
			.reanchored(&dest, &context)
			.map_err(|e| {
				tracing::error!(target: "xcm::pallet_xcm::remote_reserve_transfer_program", ?e, ?dest, ?context, "Failed to re-anchor dest_fees");
				Error::<T>::CannotReanchor
			})?;
		// identifies `dest` as seen by `reserve`
		let dest = dest.reanchored(&reserve, &context).map_err(|e| {
			tracing::error!(target: "xcm::pallet_xcm::remote_reserve_transfer_program", ?e, ?reserve, ?context, "Failed to re-anchor dest");
			Error::<T>::CannotReanchor
		})?;
		// xcm to be executed at dest
		let mut xcm_on_dest =
			Xcm(vec![BuyExecution { fees: dest_fees, weight_limit: weight_limit.clone() }]);
		// Use custom XCM on remote chain, or just default to depositing everything to beneficiary.
		let custom_xcm_on_dest = match beneficiary {
			Either::Right(custom_xcm) => custom_xcm,
			Either::Left(beneficiary) => {
				// deposit all remaining assets in holding to `beneficiary` location
				Xcm(vec![DepositAsset { assets: Wild(AllCounted(max_assets)), beneficiary }])
			},
		};
		xcm_on_dest.0.extend(custom_xcm_on_dest.into_iter());
		// xcm to be executed on reserve
		let xcm_on_reserve = Xcm(vec![
			BuyExecution { fees: reserve_fees, weight_limit },
			DepositReserveAsset { assets: Wild(AllCounted(max_assets)), dest, xcm: xcm_on_dest },
		]);
```
