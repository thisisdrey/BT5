Confirmed: the `create` extrinsic never validates any relationship between `cap` and `T::MinContribution`, so the same lower-bound/upper-bound interaction bug from the report exists in `do_contribute`.

### Title
Crowdloan `cap` vs `MinContribution` interaction permanently strands unusable remaining capacity - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
`Crowdloan::create` accepts any `cap` value with no relation to `T::MinContribution`, and `do_contribute` enforces `value >= T::MinContribution::get()` followed by `fund.raised + value <= fund.cap`. When `cap - fund.raised` drops below `MinContribution`, every possible contribution is rejected by one of the two checks, permanently stranding the remaining crowdloan capacity — an on-chain analog of the reported vault `_deposit()` bug.

### Finding Description
In `do_contribute`, the two checks are: [1](#0-0) 
```rust
ensure!(value >= T::MinContribution::get(), Error::<T>::ContributionTooSmall);
let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
fund.raised = fund.raised.checked_add(&value).ok_or(Error::<T>::Overflow)?;
ensure!(fund.raised <= fund.cap, Error::<T>::CapExceeded);
```
This mirrors exactly the vulnerable pattern in the report: a fixed lower bound (`minimumSupply`/`MinContribution`) and a fixed upper bound (`capacity`/`cap`) that are checked independently, without accounting for their interaction as the accumulated total approaches the cap.

`Crowdloan::create` never validates that `cap` is a multiple of, or otherwise compatible with, `MinContribution`: [2](#0-1) 

Once `cap - fund.raised < MinContribution`, any contribution attempt fails: values `< MinContribution` are rejected by `ContributionTooSmall`, and values `>= MinContribution` overshoot `cap` and are rejected by `CapExceeded`. No code path adjusts the minimum requirement based on remaining headroom, exactly as described in the external report's recommended fix ("adjusting the minimum deposit requirement based on the remaining capacity").

This state is trivially reachable by an unprivileged contributor: the last contributor before the gap simply needs to bring `fund.raised` to within `[cap - MinContribution + 1, cap - 1]`, which requires no special permission — any signed account calling `contribute` can do it (accidentally or intentionally), including the contributor sizing their own last contribution to leave an unfillable gap.

### Impact Explanation
The affected crowdloan can never reach its configured `cap`, permanently locking out the remaining capacity for the life of the fund. Because parachain slot auction bids are placed with `fund.raised` (see `T::Auctioneer::place_bid(... fund.raised)` in `on_initialize`), this directly caps the maximum bid a parachain can ever place below its intended `cap`, degrading the parachain's competitiveness in slot auctions — a runtime bug that compromises the intended behavior of the crowdloan mechanism (public underpriced/under-utilized capacity affecting chain-level auction outcomes). Contributed funds themselves are not lost (they remain refundable), but the vault-capacity-style guarantee ("fund can be fully utilized up to `cap`") is broken, matching the Medium-severity classification of the source report.

### Likelihood Explanation
High likelihood of occurrence in practice: any crowdloan configured with a `cap` that is not an exact multiple of `MinContribution`, combined with organic, uncoordinated contributions from many independent users, will very likely land in the unfillable gap near the end of the raise — no adversarial coordination or privileged actor is required, only ordinary use of the public `contribute`/`contribute_all` extrinsics.

### Recommendation
In `do_contribute`, when `fund.cap.saturating_sub(fund.raised) < T::MinContribution::get()`, clamp the effective minimum contribution requirement to the remaining headroom (`cap - raised`) instead of the fixed `MinContribution`, mirroring the fix already applied to the referenced Solidity vault. Alternatively, allow a final "top-up" contribution that fills exactly `cap - raised` regardless of `MinContribution`, and/or validate at `create` time that `cap` is consistent with `MinContribution` to avoid unfillable remainders by construction.

### Proof of Concept
1. Runtime configures `T::MinContribution = 100`.
2. Call `Crowdloan::create(origin, para, cap = 1000, ...)`.
3. Contributors call `contribute` repeatedly until `fund.raised = 950` (e.g., contributions of 100 × 9 + 50, or any combination summing to 950 — each individual call still satisfies `value >= 100` until the final one, which can be exactly `50` only if a prior state already allowed it, but more directly: contributors deposit `9 × 100 = 900`, then one contributor deposits `50` — wait, `50 < MinContribution` is rejected, so instead assume contributions sum to `950` via `9×100 + 50` is impossible under the check; realistically use `19×50=950` with a lower `MinContribution=50` scaled example, or directly: with `MinContribution=100`, contributors deposit `9×100=900`, then a contributor deposits `50` fails `ContributionTooSmall`; but multiple valid deposits summing to 950, e.g. `9×100 + 1×50` cannot pass minimum-check for the `50`, so instead the raised total naturally lands at 950 via `19×50` is blocked entirely — the simplest reproducible case is contributors depositing `100` nine times (`raised = 900`), then a contributor deposits `100` again bringing `raised = 1000` exactly (no gap). To force the gap, one contributor calls `contribute_all` with a balance of `50` when allowed by a prior larger `MinContribution` change via `set_configs`-equivalent, or simply set `cap = 950` and `MinContribution = 100`, so `9×100=900`, remaining `50 < 100`).
4. With `cap = 950`, `MinContribution = 100`: after `900` raised, remaining capacity is `50`. Any further `contribute(value)` with `value < 100` fails `Error::ContributionTooSmall`; any `value >= 100` fails `Error::CapExceeded` since `900+100=1000 > 950`.
5. The crowdloan is now permanently stuck at `raised = 900` out of `cap = 950`; the 50-unit gap can never be filled, and the parachain can only ever bid `900` instead of the intended `950`. [3](#0-2)

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L343-349)
```rust
					let result = T::Auctioneer::place_bid(
						Self::fund_account_id(fund.fund_index),
						para_id,
						fund.first_period,
						fund.last_period,
						fund.raised,
					);
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L369-434)
```rust
		pub fn create(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
			#[pallet::compact] cap: BalanceOf<T>,
			#[pallet::compact] first_period: LeasePeriodOf<T>,
			#[pallet::compact] last_period: LeasePeriodOf<T>,
			#[pallet::compact] end: BlockNumberFor<T>,
			verifier: Option<MultiSigner>,
		) -> DispatchResult {
			let depositor = ensure_signed(origin)?;
			let now = frame_system::Pallet::<T>::block_number();

			ensure!(first_period <= last_period, Error::<T>::LastPeriodBeforeFirstPeriod);
			let last_period_limit = first_period
				.checked_add(&((SlotRange::LEASE_PERIODS_PER_SLOT as u32) - 1).into())
				.ok_or(Error::<T>::FirstPeriodTooFarInFuture)?;
			ensure!(last_period <= last_period_limit, Error::<T>::LastPeriodTooFarInFuture);
			ensure!(end > now, Error::<T>::CannotEndInPast);

			// Here we check the lease period on the ending block is at most the first block of the
			// period after `first_period`. If it would be larger, there is no way we could win an
			// active auction, thus it would make no sense to have a crowdloan this long.
			let (lease_period_at_end, is_first_block) =
				T::Auctioneer::lease_period_index(end).ok_or(Error::<T>::NoLeasePeriod)?;
			let adjusted_lease_period_at_end = if is_first_block {
				lease_period_at_end.saturating_sub(One::one())
			} else {
				lease_period_at_end
			};
			ensure!(adjusted_lease_period_at_end <= first_period, Error::<T>::EndTooFarInFuture);

			// Can't start a crowdloan for a lease period that already passed.
			if let Some((current_lease_period, _)) = T::Auctioneer::lease_period_index(now) {
				ensure!(first_period >= current_lease_period, Error::<T>::FirstPeriodInPast);
			}

			// There should not be an existing fund.
			ensure!(!Funds::<T>::contains_key(index), Error::<T>::FundNotEnded);

			let manager = T::Registrar::manager_of(index).ok_or(Error::<T>::InvalidParaId)?;
			ensure!(depositor == manager, Error::<T>::InvalidOrigin);
			ensure!(T::Registrar::is_registered(index), Error::<T>::InvalidParaId);

			let fund_index = NextFundIndex::<T>::get();
			let new_fund_index = fund_index.checked_add(1).ok_or(Error::<T>::Overflow)?;

			let deposit = T::SubmissionDeposit::get();

			frame_system::Pallet::<T>::inc_providers(&Self::fund_account_id(fund_index));
			CurrencyOf::<T>::reserve(&depositor, deposit)?;

			Funds::<T>::insert(
				index,
				FundInfo {
					depositor,
					verifier,
					deposit,
					raised: Zero::zero(),
					end,
					cap,
					last_contribution: LastContribution::Never,
					first_period,
					last_period,
					fund_index,
				},
			);
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L756-759)
```rust
		ensure!(value >= T::MinContribution::get(), Error::<T>::ContributionTooSmall);
		let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
		fund.raised = fund.raised.checked_add(&value).ok_or(Error::<T>::Overflow)?;
		ensure!(fund.raised <= fund.cap, Error::<T>::CapExceeded);
```
