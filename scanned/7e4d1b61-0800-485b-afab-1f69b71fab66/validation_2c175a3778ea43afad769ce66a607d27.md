### Title
Live `total_issuance()` re-query lets an unprivileged burner shrink the `SuperMajorityApprove`/`SuperMajorityAgainst` electorate mid-referendum, lowering the pass threshold on demand - (File: `substrate/frame/democracy/src/lib.rs`, `substrate/frame/democracy/src/vote_threshold.rs`, `substrate/frame/balances/src/lib.rs`)

### Summary
`pallet-democracy` decides whether a referendum passes by calling `VoteThreshold::approved(tally, electorate)` where `electorate` is `T::Currency::total_issuance()` fetched fresh at bake time [1](#0-0) . `pallet-balances` exposes a fully public, permissionless `burn` extrinsic that irreversibly reduces `TotalIssuance` for any signed caller [2](#0-1) . This mirrors the Augur `C08` pattern exactly: a public, anyone-callable function (`updateTotalTheoreticalSupply`/here, `burn`) permanently shrinks a supply-like value, and a second public/automatic function (`updateForkValues`/here, `bake_referendum`) re-derives a pass/fail threshold from that shrinking value with no snapshot or freeze, so the “goal” a proposal must clear becomes a moving, ever-easier target during the very process meant to judge it.

### Finding Description
- `VoteThreshold::approved` computes approval using `sqrt_electorate = electorate.integer_sqrt()` where `electorate` is total issuance, comparing it against `sqrt_voters` (turnout) via `compare_rationals` [3](#0-2) .
- `bake_referendum`, called from `begin_block` when a referendum matures, queries `T::Currency::total_issuance()` live, at conclusion time, not at proposal/voting-start time [4](#0-3) .
- `pallet_balances::Pallet::burn` is `#[pallet::call_index(10)]`, requires only `ensure_signed(origin)`, and unconditionally reduces `TotalIssuance` by the burned amount — this is explicitly documented as different from sending to a burn address because it "reduce[s] total issuance by the amount burned" [5](#0-4) . The test `burn_works` confirms `TotalIssuance` decreases by exactly the burned amount with no other precondition [6](#0-5) .
- Because `electorate` (i.e., `sqrt_electorate`) shrinks whenever anyone burns tokens, and `compare_rationals` compares `nays/sqrt_voters` vs `ayes/sqrt_electorate` (`SuperMajorityApprove`) or the mirrored ratio for `SuperMajorityAgainst`, a smaller `sqrt_electorate` makes it easier for a given `ayes` turnout to satisfy `approved()` for `SuperMajorityApprove`, and easier to defeat a `SuperMajorityAgainst`-guarded proposal, without any additional votes being cast. This is the exact structural analog of the Augur bug: an ever-decreasing “total” recomputed from a public burn action, feeding a threshold-approval calculation with no snapshot taken at the start of the decision window and no freeze once the vote is live.
- Existing guards do not stop this path: `burn` has no relation to democracy and no rate limit tied to referenda; `bake_referendum`/`begin_block` never snapshot `total_issuance()` at `launch_next`/`inject_referendum` time, nor is `total_issuance` locked once a referendum enters its voting/maturing window. There is no `require`-style check equivalent to `!isForking()` guarding threshold recomputation against manipulation while a referendum is in flight.

### Impact Explanation
This is a runtime bug that compromises intended governance behavior: an unprivileged account can unilaterally lower the quorum/support bar a live referendum must clear (or must be defeated by) by simply burning liquid tokens they control, at any point up to the block the referendum matures. This can let a proposal that legitimately lacked sufficient community support pass anyway (`SuperMajorityApprove`), or let a proposal that should have been defeated by `SuperMajorityAgainst` survive, letting the attacker warp the referendum outcome — a compromise of the intended governance invariant that outcomes reflect proportional, undiluted stake-weighted consent. Note `pallet-democracy` is largely legacy/superseded by `pallet-referenda` in current runtimes, and its threshold curves in `pallet-referenda` are computed against `Curve`/track parameters rather than raw electorate size in the same way, so blast radius is limited to chains still running `pallet-democracy` with `VoteThreshold`-style tracks and the standard `pallet_balances::burn` extrinsic enabled.

### Likelihood Explanation
Moderate-to-low in practice, but structurally trivial to trigger: the attacker only needs (a) any account with a burnable balance, and (b) an active, undecided referendum on a `pallet-democracy`-enabled chain. No collusion, no privileged role, no malicious relayer/validator/collator is required — purely a public extrinsic call (`burn`) interacting with a public computation path (`bake_referendum` → `VoteThreshold::approved`). The main constraint is the magnitude of total issuance versus the margin needed to flip a referendum's outcome, which determines how much must be burned; this is comparable to the Augur scenario's constraint on migrated REP volume needed to move `forkReputationGoal`.

### Recommendation
Snapshot the electorate (`total_issuance`) once, at referendum launch/injection time, and use that frozen value throughout the referendum's lifetime rather than re-querying `T::Currency::total_issuance()` at `bake_referendum` time. Alternatively/additionally, disallow or delay-account for issuance changes that occur strictly during a referendum's decision window when computing `approved()`, analogous to the `require(!isForking())` guard suggested for `updateForkValues`. Add unit tests asserting that burning tokens after a referendum has been injected but before it concludes does not change the referendum's pass/fail outcome relative to a baseline electorate captured at injection time.

### Proof of Concept
1. On a chain running `pallet-democracy` with `VoteThreshold::SuperMajorityApprove` and `pallet-balances::burn` enabled, note `TotalIssuance` = `I0`.
2. Inject/launch a referendum; voters cast `aye`/`nay` producing a `Tally { ayes, nays, turnout }` that currently fails `VoteThreshold::SuperMajorityApprove::approved(tally, I0)` per `compare_rationals(nays, sqrt(turnout), ayes, sqrt(I0))` [3](#0-2) .
3. Before the referendum matures, any account (not necessarily a voter) calls `Balances::burn(origin, large_amount, false)`, reducing `TotalIssuance` to `I1 < I0` [7](#0-6) .
4. At maturity, `begin_block` → `bake_referendum` re-reads `total_issuance()` = `I1` and calls `status.threshold.approved(status.tally, I1)` [1](#0-0) ; with smaller `sqrt(I1)`, the same unchanged `tally` now satisfies `compare_rationals(nays, sqrt(turnout), ayes, sqrt(I1))`, and the referendum is `Passed` where it would otherwise have been `NotPassed` with the original `I0`.
5. This is directly demonstrable by adapting the existing `should_work` unit test in `vote_threshold.rs` [8](#0-7) : the same `Tally { ayes: 60, nays: 50, turnout: 110 }` fails against electorate `210` but passes against a smaller electorate, confirming that shrinking `electorate` alone (achievable via public `burn`) flips the approval result with no change in actual votes.

### Citations

**File:** substrate/frame/democracy/src/lib.rs (L1597-1603)
```rust
	fn bake_referendum(
		now: BlockNumberFor<T>,
		index: ReferendumIndex,
		status: ReferendumStatus<BlockNumberFor<T>, BoundedCallOf<T>, BalanceOf<T>>,
	) -> bool {
		let total_issuance = T::Currency::total_issuance();
		let approved = status.threshold.approved(status.tally, total_issuance);
```

**File:** substrate/frame/balances/src/lib.rs (L850-874)
```rust
		/// Burn the specified liquid free balance from the origin account.
		///
		/// If the origin's account ends up below the existential deposit as a result
		/// of the burn and `keep_alive` is false, the account will be reaped.
		///
		/// Unlike sending funds to a _burn_ address, which merely makes the funds inaccessible,
		/// this `burn` operation will reduce total issuance by the amount _burned_.
		#[pallet::call_index(10)]
		#[pallet::weight(if *keep_alive {T::WeightInfo::burn_keep_alive()} else {T::WeightInfo::burn_allow_death()})]
		pub fn burn(
			origin: OriginFor<T>,
			#[pallet::compact] value: T::Balance,
			keep_alive: bool,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;
			let preservation = if keep_alive { Preserve } else { Expendable };
			<Self as fungible::Mutate<_>>::burn_from(
				&source,
				value,
				preservation,
				Precision::Exact,
				Polite,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/democracy/src/vote_threshold.rs (L103-118)
```rust
	fn approved(&self, tally: Tally<Balance>, electorate: Balance) -> bool {
		let sqrt_voters = tally.turnout.integer_sqrt();
		let sqrt_electorate = electorate.integer_sqrt();
		if sqrt_voters.is_zero() {
			return false;
		}
		match *self {
			VoteThreshold::SuperMajorityApprove => {
				compare_rationals(tally.nays, sqrt_voters, tally.ayes, sqrt_electorate)
			},
			VoteThreshold::SuperMajorityAgainst => {
				compare_rationals(tally.nays, sqrt_electorate, tally.ayes, sqrt_voters)
			},
			VoteThreshold::SimpleMajority => tally.ayes > tally.nays,
		}
	}
```

**File:** substrate/frame/democracy/src/vote_threshold.rs (L125-131)
```rust
	#[test]
	fn should_work() {
		assert!(!VoteThreshold::SuperMajorityApprove
			.approved(Tally { ayes: 60, nays: 50, turnout: 110 }, 210));
		assert!(VoteThreshold::SuperMajorityApprove
			.approved(Tally { ayes: 100, nays: 50, turnout: 150 }, 210));
	}
```

**File:** substrate/frame/balances/src/tests/dispatchable_tests.rs (L339-362)
```rust
#[test]
fn burn_works() {
	ExtBuilder::default().build().execute_with(|| {
		// Prepare account with initial balance
		let (account, init_balance) = (1, 37);
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), account, init_balance));
		let init_issuance = pallet_balances::TotalIssuance::<Test>::get();
		let (keep_alive, allow_death) = (true, false);

		// 1. Cannot burn more than what's available
		assert_noop!(
			Balances::burn(Some(account).into(), init_balance + 1, allow_death),
			TokenError::FundsUnavailable,
		);

		// 2. Burn some funds, without reaping the account
		let burn_amount_1 = 1;
		assert_ok!(Balances::burn(Some(account).into(), burn_amount_1, allow_death));
		System::assert_last_event(RuntimeEvent::Balances(Event::Burned {
			who: account,
			amount: burn_amount_1,
		}));
		assert_eq!(pallet_balances::TotalIssuance::<Test>::get(), init_issuance - burn_amount_1);
		assert_eq!(Balances::total_balance(&account), init_balance - burn_amount_1);
```
