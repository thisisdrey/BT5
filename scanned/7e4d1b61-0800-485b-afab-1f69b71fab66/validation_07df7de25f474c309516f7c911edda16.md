Based on my investigation of the `substrate/frame/psm/src/lib.rs` pallet, I found a concrete local analog. I was unable to complete reading the `internal_to_external`/`external_to_internal` conversion helper bodies before the tool budget ran out, so the exact truncation bound (dependent on `MAX_DECIMALS_DIFF` and per-pair decimal deltas) is inferred from the surrounding doc comments and events rather than fully verified line-by-line — this should be confirmed by reading those two conversion functions directly.

### Title
PSM `redeem` charges the redemption fee on the full requested amount while only a smaller, decimal-truncated amount is actually burned/exchanged - (File: substrate/frame/psm/src/lib.rs)

### Summary
`Pallet::redeem` computes the redemption fee from the user-supplied `internal_amount` before the amount is converted through the external asset's decimals and rounded back, so the fee is charged on a value larger than what the protocol actually processes for the user. This is the same broken invariant as H-35 in Astaria's `AuctionHouse._handleIncomingPayment`: fee is derived from the "requested"/"transfer" amount instead of the amount actually consumed by the operation.

### Finding Description
In `redeem` [1](#0-0) :

```
let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
let fee = fee_rate.mul_ceil(internal_amount);
let internal_net = internal_amount.saturating_sub(fee);

let external_out =
    Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
...
let effective_internal_net =
    Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;
```

The fee is `fee_rate.mul_ceil(internal_amount)` — calculated on the caller's full requested `internal_amount`. The amount that is actually converted to `external_out` and then round-tripped back is `effective_internal_net`, which the code's own comment acknowledges can be strictly less than `internal_net` due to decimal-precision truncation ("`effective_internal_net`... Any truncation dust stays in the caller's internal balance, symmetric with `mint`") [2](#0-1) .

The total value actually moved out of the user's control per redemption is `fee + effective_internal_net` (fee transferred to `fee_destination`, `effective_internal_net` burned) [3](#0-2) . Whenever `effective_internal_net < internal_net` (i.e., whenever the internal→external decimal conversion truncates), the effective fee rate paid by the user, `fee / (fee + effective_internal_net)`, is strictly greater than the configured `fee_rate = fee / (fee + internal_net)`. The user is charged the nominal fee on an amount inflated by conversion dust that is never actually redeemed for them in that call — mirroring the Astaria bug where `initiatorPayment` was computed on `transferAmount` instead of the amount actually applied to liens.

This differs from `mint`, where the fee is computed on `internal_equivalent`, which is exactly what the debt ledger and event track and what is minted/transferred — `mint` does not have this asymmetry. `redeem` breaks the pattern by taxing the pre-truncation `internal_amount` rather than the post-truncation `effective_internal_net`.

### Impact Explanation
Every unprivileged, signed caller of `redeem` is systematically overcharged fees whenever `internal_decimals != ext_decimals` in a way that produces non-trivial truncation on the `internal_net → external_out → effective_internal_net` round trip. This is a live-scope "runtime bug that compromises intended behavior" per the impact gate: the configured `RedemptionFee` (a value governance explicitly sets as the fee rate) is silently exceeded on-chain for ordinary users, and the excess is captured by `fee_destination`, i.e., a form of unbacked/incorrect fee extraction from user funds rather than a benign rounding dust that stays with the user. Because `PsmDebt` is decremented only by `effective_internal_net` (not `internal_net`), the debt-accounting and reserve backing stay internally consistent, but the *fee* line is inconsistent with the actual redeemed amount, meaning `Redeemed.internal_fee` no longer reflects `fee_rate` applied to `Redeemed.internal_consumed`'s exchanged portion as documented.

### Likelihood Explanation
This triggers on ordinary, permissionless usage — no malicious peer, relayer, governance, or admin action is needed, satisfying the "unprivileged attacker/normal user" requirement of the impact gate. It occurs deterministically whenever a PSM instance pairs an internal asset with an external asset of differing decimals (a normal, expected configuration per the pallet's own multi-decimals design, bounded by `MAX_DECIMALS_DIFF = 24`), and the user's chosen `internal_amount` does not land on a decimal boundary that survives the round trip losslessly — which is the common case for arbitrary user-chosen amounts.

### Recommendation
Compute the redemption fee on the amount that is actually exchanged, not on the raw `internal_amount`. Concretely, first compute `external_out` and `effective_internal_net` from `internal_amount` (or from `internal_amount` net of an appropriately derived provisional split), then derive `fee` as `fee_rate.mul_ceil(effective_internal_net + fee)`-consistent quantity (e.g., solve fee from the post-truncation redeemed amount, analogous to how `mint` derives its fee from `internal_equivalent`), ensuring `fee / (fee + effective_internal_net) == fee_rate` rather than `fee / (fee + internal_net) == fee_rate`.

### Proof of Concept
1. Governance creates a PSM with `internal_asset` at 18 decimals and approves `external_asset` at 6 decimals, with `RedemptionFee = 0.5%`.
2. A user calls `redeem(internal_asset, external_asset, internal_amount, max_fee)` with an `internal_amount` chosen so that after fee deduction, `internal_net` does not divide evenly onto the external asset's coarser (6-decimal) precision (trivial to arrange, since 12 orders of magnitude of internal precision get truncated away when converting to 6-decimal external units).
3. `fee = fee_rate.mul_ceil(internal_amount)` is computed and transferred from the user to `fee_destination`.
4. `external_out = internal_to_external(internal_net, ...)` truncates the sub-6-decimal remainder.
5. `effective_internal_net = external_to_internal(external_out, ...)` is strictly less than `internal_net` by the truncated remainder.
6. Only `fee + effective_internal_net` is actually removed from the user's total (`Redeemed.internal_consumed`), while the true effective fee rate `fee / (fee + effective_internal_net)` now exceeds the configured `fee_rate`, reproducing the H-35 "fee charged on more than what was actually used" pattern.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L828-846)
```rust
			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);

			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;
```

**File:** substrate/frame/psm/src/lib.rs (L857-876)
```rust
			if !fee.is_zero() {
				T::Fungibles::transfer(
					internal_asset.clone(),
					&who,
					&info.fee_destination,
					fee,
					Preservation::Expendable,
				)?;
			}

			if !effective_internal_net.is_zero() {
				T::Fungibles::burn_from(
					internal_asset.clone(),
					&who,
					effective_internal_net,
					Preservation::Expendable,
					Precision::Exact,
					Fortitude::Polite,
				)?;
			}
```
