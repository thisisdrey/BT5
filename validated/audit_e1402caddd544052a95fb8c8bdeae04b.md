This is exactly the `remote_reserve_transfer_program` function used by the legacy `reserve_transfer_assets`/`limited_reserve_transfer_assets` extrinsics in `pallet-xcm`, which is the closest local analog to the reported "equal split" bug pattern.

### Title
`reserve_transfer_assets` unconditionally halves user fee amount between reserve and destination hops - ([File: polkadot/xcm/pallet-xcm/src/lib.rs])

### Summary
`Pallet::remote_reserve_transfer_program` always splits the caller-supplied fee asset exactly in half via `halve_fees`, using `fees_half_1` for `BuyExecution` on the intermediate reserve chain and `fees_half_2` for `BuyExecution` on the final destination chain. The user has no way to specify how much fee should be spent on each hop; the split is hard-coded to 50/50 regardless of the actual (and possibly very different) execution costs on the reserve chain versus the destination chain.

### Finding Description
`remote_reserve_transfer_program` is the multi-hop reserve-transfer path used by `reserve_transfer_assets` / `limited_reserve_transfer_assets`, when `assets` and `fees` share a remote reserve different from both origin and destination (e.g. parachain → Asset Hub (reserve) → other parachain). [1](#0-0) 
splits the single `fees` asset provided by the caller: [2](#0-1) 
`halve_fees` divides the fee amount by 2 (with remainder going to the second half), and this exact half is then reanchored and used as the `BuyExecution` fee both at the `reserve` chain and at the `dest` chain: [3](#0-2) 

This is structurally identical to the reported bug class: a single total fee amount is mechanically divided among multiple cross-chain legs (here, two hops: reserve chain execution and destination chain execution) instead of letting the caller specify how much fee should be spent on each leg independently. If the reserve chain's `BuyExecution` weight/fee requirement is greater than half of the total fee (e.g. congestion, heavier `DepositReserveAsset` weight, or a reserve chain with higher execution costs), the reserve-chain `BuyExecution` can fail with `TooExpensive`, trapping/burning the withdrawn fee assets that were already irreversibly `WithdrawAsset`'d and forwarded via `InitiateReserveWithdraw`. Conversely, if the destination-chain execution needs more than half, the deposit at the final destination fails, and remaining assets are trapped at destination (no `RefundSurplus`/`DepositAsset(Wild(All))` catch-all is present in `xcm_on_dest`/`xcm_on_reserve` for the failure case beyond `AllCounted`, but `BuyExecution` failure aborts the program at that hop, so assets sent so far may become inaccessible to the user without a rescue mechanism specific to this path).

Because the caller can only supply a single `fees` asset amount for the whole path (via `reserve_transfer_assets`/`limited_reserve_transfer_assets` extrinsic signatures, which do not expose per-hop fee parameters for this legacy call), users are forced to either overpay significantly (guess a total large enough that half is still sufficient for the more expensive hop) or risk execution failure/fund loss on a hop that needs more than 50%.

### Impact Explanation
This does not "steal" funds outright, but it is a genuine functional/economic bug affecting public, unprivileged cross-chain transfers: assets can become stuck/trapped mid-route or a transfer can revert with the intermediate reserve-chain withdrawal already having consumed the withdrawn assets from the sender, forcing reliance on `claim_assets` to recover trapped assets (extra friction, and only works if the assets are trapped rather than fully consumed). Given `reserve_transfer_assets` is a widely used, unprivileged, user-facing extrinsic for multi-hop reserve transfers, this fits "public underpriced work" / fund-lock impact criteria for cross-chain message routing.

### Likelihood Explanation
Medium likelihood: any user transferring assets through a reserve chain to a destination whose execution costs are not equal to the reserve chain's costs (a very common asymmetric case, e.g. cheap reserve-chain forwarding vs. expensive destination chain deposit logic) will hit this. It requires no malicious actor — it's triggered by ordinary usage with normal fee/weight variance across chains.

### Recommendation
Add an explicit per-hop fee split parameter (or use the newer `transfer_assets_using_type_and_then` / `InitiateTransfer` instruction, which already supports separate `remote_fees` per hop) instead of relying on `halve_fees`'s fixed 50/50 split in `remote_reserve_transfer_program`. At minimum, deprecate/guard the legacy `reserve_transfer_assets` path for routes requiring a remote reserve hop with asymmetric costs, and document/require callers to use the newer explicit-fee-per-hop extrinsic.

### Proof of Concept
1. Configure a parachain-to-parachain reserve transfer where `dest` requires significantly higher execution weight/fee for `BuyExecution` (e.g., due to a heavier `DepositAsset` beneficiary lookup or additional custom XCM on dest) than the `reserve` chain's `DepositReserveAsset` forward.
2. Call `pallet_xcm::reserve_transfer_assets` with `fees` sized so that legitimate total cost fits, but the actual required split is 70/30 (dest/reserve) rather than 50/50.
3. Observe `remote_reserve_transfer_program`'s `halve_fees` splits the amount 50/50; the `BuyExecution` at `xcm_on_dest` fails with `TooExpensive` because `dest_fees` (half) is insufficient, while `assets` and `fees_half_1` have already been withdrawn/forwarded via `InitiateReserveWithdraw` on the reserve chain, leaving assets trapped at/after the reserve hop and requiring `claim_assets` recovery, or being irrecoverable to the sender's origin chain. [4](#0-3)

### Citations

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2580-2644)
```rust
	// function assumes fees and assets have the same remote reserve
	fn remote_reserve_transfer_program(
		origin: Location,
		reserve: Location,
		beneficiary: Either<Location, Xcm<()>>,
		dest: Location,
		assets: Vec<Asset>,
		fees: Asset,
		weight_limit: WeightLimit,
	) -> Result<Xcm<<T as Config>::RuntimeCall>, Error<T>> {
		let value = (origin, assets);
		ensure!(T::XcmReserveTransferFilter::contains(&value), Error::<T>::Filtered);
		let (_, assets) = value;

		let max_assets = assets.len() as u32;
		let context = T::UniversalLocation::get();
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
		Ok(Xcm(vec![
			WithdrawAsset(assets.into()),
			SetFeesMode { jit_withdraw: true },
			InitiateReserveWithdraw {
				assets: Wild(AllCounted(max_assets)),
				reserve,
				xcm: xcm_on_reserve,
			},
		]))
	}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L2797-2809)
```rust
	/// Halve `fees` fungible amount.
	pub(crate) fn halve_fees(fees: Asset) -> Result<(Asset, Asset), Error<T>> {
		match fees.fun {
			Fungible(amount) => {
				let fee1 = amount.saturating_div(2);
				let fee2 = amount.saturating_sub(fee1);
				ensure!(fee1 > 0, Error::<T>::FeesNotMet);
				ensure!(fee2 > 0, Error::<T>::FeesNotMet);
				Ok((Asset::from((fees.id.clone(), fee1)), Asset::from((fees.id.clone(), fee2))))
			},
			NonFungible(_) => Err(Error::<T>::FeesNotMet),
		}
	}
```
