### Title
Crowdloan Contributions Can Be Permanently Blocked Near the Fund Cap Due to Conflicting `MinContribution`/`cap` Checks - ([File: polkadot/runtime/common/src/crowdloan/mod.rs])

### Summary
`Pallet::do_contribute` in the crowdloan pallet enforces two independent bounds on a contribution: a lower bound (`T::MinContribution`) and an upper bound (`fund.cap`). Exactly like the reported `fundContract.sol` `_deposit()` bug, these two checks can conflict once the fund's raised amount is within `MinContribution` of the `cap`: no value satisfies "≥ MinContribution" without also violating "raised + value ≤ cap". The remaining headroom under the cap becomes permanently unusable for that crowdloan.

### Finding Description
`do_contribute` performs the checks in this order: [1](#0-0) 

```rust
ensure!(value >= T::MinContribution::get(), Error::<T>::ContributionTooSmall);
let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
fund.raised = fund.raised.checked_add(&value).ok_or(Error::<T>::Overflow)?;
ensure!(fund.raised <= fund.cap, Error::<T>::CapExceeded);
```

`MinContribution` is a fixed, chain-wide pallet constant (e.g. `100 * CENTS` on Westend) [2](#0-1)  and `cap` is fixed per fund at `create` time [3](#0-2) . Neither value is adjusted based on the other as the fund fills up. Once `fund.cap - fund.raised < T::MinContribution::get()`, any contribution attempt fails one of the two checks:
- A contribution `< MinContribution` fails `ContributionTooSmall`.
- A contribution `≥ MinContribution` makes `fund.raised + value > fund.cap`, failing `CapExceeded`.

This is structurally identical to the `_deposit()` flaw in the report: `require(vaultSetting.minimumSupply <= _amount, ...)` combined with `require(nav + _amount <= capacity, ...)`. The existing guards (`ContributionTooSmall`, `CapExceeded`) do not account for this interaction — there is no logic anywhere in `do_contribute`, `edit`, or `create` that shrinks the effective minimum contribution when remaining headroom under `cap` is small.

### Impact Explanation
The remaining slice of the crowdloan's `cap` (up to `MinContribution - 1` less than `MinContribution`) becomes permanently unreachable for that fund's lifetime — no one, not even the depositor, can top off the fund to its intended cap. This directly parallels the reported issue's impact: "prevents full capital deployment and introduces inefficiencies in fund management," here applied to parachain crowdloan capital raised for slot auctions. Because crowdloan funds are meant to reach `cap` to maximize competitive bidding power for a parachain slot auction, this dead-zone can measurably and permanently under-fund a crowdloan's bidding potential, and is trivially reachable by any unprivileged contributor (or occurs naturally without any adversarial action) once the fund nears its cap.

### Likelihood Explanation
This requires no privileged actor, governance action, or malicious peer — any signed account calling `contribute`/`contribute_all` at a normal time can trigger or simply encounter this state as the fund fills up organically. It is a deterministic function of `cap`, `MinContribution`, and `raised`, so it will occur whenever `cap % ...` combinations leave a remainder smaller than `MinContribution`, or when late contributors intentionally (or accidentally) leave such a remainder. `MinContribution` is a global constant on the runtime, not scaled per-fund, so this dead-zone is common for arbitrary `cap` values chosen by parachain teams at `create`.

### Recommendation
Adjust `do_contribute` so the effective lower bound relaxes when remaining capacity is smaller than `MinContribution`, mirroring the report's fix recommendation: allow a contribution equal to exactly `fund.cap - fund.raised` even if it's below `MinContribution`, e.g.:

```rust
let remaining = fund.cap.saturating_sub(fund.raised);
let min_required = T::MinContribution::get().min(remaining);
ensure!(value >= min_required, Error::<T>::ContributionTooSmall);
```

This way the last remaining amount under `cap` can always be filled by a contribution exactly matching the remaining headroom.

### Proof of Concept
1. `Crowdloan::create` a fund with `cap = 1000`, `MinContribution = 100` (as configured on Westend/Polkadot runtimes).
2. Contributors bring `fund.raised` to `950` via any sequence of valid contributions (each ≥ 100, cumulative ≤ 1000 is satisfiable, e.g., 950 in one shot or via 100+100+...+50 is itself impossible for the last 50 — showing the bug directly on the very last increment).
3. Any further call to `contribute(index, 50, ...)` fails `ContributionTooSmall` (50 < 100).
4. Any call to `contribute(index, 100, ...)` fails `CapExceeded` (950 + 100 = 1050 > 1000).
5. The crowdloan permanently caps out at `950/1000`; the remaining `50` capacity can never be raised, matching the `_deposit()` capacity-lock behavior in the original report.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L420-434)
```rust
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

**File:** polkadot/runtime/westend/src/lib.rs (L1299-1303)
```rust
parameter_types! {
	pub const CrowdloanId: PalletId = PalletId(*b"py/cfund");
	pub const SubmissionDeposit: Balance = 100 * 100 * CENTS;
	pub const MinContribution: Balance = 100 * CENTS;
	pub const RemoveKeysLimit: u32 = 500;
```
