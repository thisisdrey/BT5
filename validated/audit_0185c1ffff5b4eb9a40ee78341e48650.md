## Analog Identified: Crowdloan Fund Contribution Griefing

The core broken invariant in the Stakehouse report is: **a fixed minimum unit-size check combined with a fixed maximum cap**, where an attacker can leave a "remaining space to the cap" that is smaller than the minimum unit, permanently blocking further legitimate contributions. This exact pattern exists in the Polkadot `crowdloan` pallet.

### Title
Crowdloan funds can be griefed to a permanently unreachable `cap`, blocking further contributions - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
`Pallet::do_contribute` enforces two independent, un-reconciled checks: a minimum per-contribution size (`T::MinContribution`) and a hard cap on total `raised` funds. Because these two bounds are checked independently rather than jointly, an unprivileged contributor can drive `cap - raised` to a value below `MinContribution`, after which the fund can never legitimately receive further contributions, even though it is still below its cap.

### Finding Description
`do_contribute` performs: [1](#0-0) 

```rust
ensure!(value >= T::MinContribution::get(), Error::<T>::ContributionTooSmall);
let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
fund.raised = fund.raised.checked_add(&value).ok_or(Error::<T>::Overflow)?;
ensure!(fund.raised <= fund.cap, Error::<T>::CapExceeded);
```

`contribute` and `contribute_all` are both open, unprivileged, signed extrinsics that route into this function: [2](#0-1) [3](#0-2) 

`FundInfo` carries `raised` and a fixed `cap` set at fund creation (or later via the root-only `edit` call), just as `ETHPoolLPFactory` carried `maxStakingAmountPerValidator`: [4](#0-3) 

There is no logic that widens or waives `MinContribution` when `cap - raised < MinContribution`. Any account can contribute a precise amount such that `fund.cap - fund.raised` becomes a value strictly less than `T::MinContribution::get()` (e.g., leave exactly 1 unit of headroom). From that point:
- Any contribution `>= MinContribution` fails `ensure!(fund.raised <= fund.cap, ...)` → `CapExceeded`.
- Any contribution `< MinContribution` fails the first check → `ContributionTooSmall`.

There is no code path that "rounds up" or accepts the exact remainder in that gap — the same defect pattern flagged in the Stakehouse report where `_amount >= MIN_STAKING_AMOUNT` and `totalSupply + _amount <= maxStakingAmountPerValidator` are checked independently with no exception for the final remainder.

### Impact Explanation
Once griefed, the fund can never actually reach its configured `cap` through the public `contribute`/`contribute_all` entry points, even though contributed funds remain fully refundable (no permanent fund loss). The practical harm is economic/DoS: a crowdloan intended to raise up to `cap` (e.g., to be competitive in a slot auction) is permanently prevented from doing so once the gap collapses below `MinContribution`, degrading the crowdloan's ability to reach its funding target and potentially costing the parachain its auction slot bid — a public, underpriced griefing vector against a core public-facing extrinsic, executable by any unprivileged account with a trivial cost (a single small contribution).

### Likelihood Explanation
High feasibility: the attack requires only one signed extrinsic call (`contribute`) with a precisely chosen `value`, no special privileges, no validator/collator/relayer collusion, and no race condition — deterministic arithmetic guarantees the outcome. Any contributor (malicious actor, or even the fund's own depositor by accident) can trigger it at any time during the raise period.

### Recommendation
When `fund.raised + value` would exceed `fund.cap`, clamp the accepted contribution to `fund.cap - fund.raised` (accepting the partial remainder) instead of unconditionally rejecting values below `MinContribution` once fewer than `MinContribution` units remain to the cap — mirroring the Stakehouse-recommended fix of allowing a final under-`MIN_STAKING_AMOUNT` deposit that exactly completes the cap.

### Proof of Concept
1. `Crowdloan::create` a fund with `cap = 1000`, `MinContribution = 10`.
2. Contributor A calls `contribute(index, 991)` → `fund.raised = 991`, `cap - raised = 9 < MinContribution`.
3. Any subsequent `contribute(index, v)`:
   - `v = 9` → fails `ContributionTooSmall` (9 < 10).
   - `v = 10` → fails `CapExceeded` (991 + 10 = 1001 > 1000).
4. The fund is now permanently stuck at 991/1000 raised for the remainder of its lifetime; no further contribution can ever succeed, matching the exact griefing mechanic described in the referenced Stakehouse finding. [5](#0-4)

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L148-155)
```rust
	pub deposit: Balance,
	/// The total amount raised.
	pub raised: Balance,
	/// Block number after which the funding must have succeeded. If not successful at this number
	/// then everyone may withdraw their funds.
	pub end: BlockNumber,
	/// A hard-cap on the amount that may be contributed.
	pub cap: Balance,
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L444-454)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::contribute())]
		pub fn contribute(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
			#[pallet::compact] value: BalanceOf<T>,
			signature: Option<MultiSignature>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			Self::do_contribute(who, index, value, signature, KeepAlive)
		}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L658-668)
```rust
		#[pallet::call_index(8)]
		#[pallet::weight(T::WeightInfo::contribute())]
		pub fn contribute_all(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
			signature: Option<MultiSignature>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let value = CurrencyOf::<T>::free_balance(&who);
			Self::do_contribute(who, index, value, signature, AllowDeath)
		}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L756-759)
```rust
		ensure!(value >= T::MinContribution::get(), Error::<T>::ContributionTooSmall);
		let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
		fund.raised = fund.raised.checked_add(&value).ok_or(Error::<T>::Overflow)?;
		ensure!(fund.raised <= fund.cap, Error::<T>::CapExceeded);
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L1409-1413)
```rust
			// Cannot contribute past the limit
			assert_noop!(
				Crowdloan::contribute(RuntimeOrigin::signed(2), para, 900, None),
				Error::<Test>::CapExceeded
			);
```
