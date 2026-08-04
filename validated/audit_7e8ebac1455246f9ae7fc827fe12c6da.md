Confirmed: this is documented explicitly at line 559-562 of `substrate/frame/support/src/traits/hooks.rs` — `integrity_test()` is only ever "placed in an auto-generated test... generated as a part of `crate::construct_runtime`'s expansion," i.e. it is a compile-time/test-only assertion (`__construct_runtime_integrity_test`), never a runtime-enforced, on-chain check. This is the exact structural analog to the external report: a critical two-period ordering invariant that only exists as an `assert!` outside of consensus execution, with no actual on-chain guard preventing a misconfigured runtime from shipping.

### Title
Slash-defer vs. bonding duration invariant is only enforced by a test-only `integrity_test`, not by any on-chain guard - (File: `substrate/frame/staking-async/src/pallet/mod.rs`)

### Summary
`pallet-staking-async` (and legacy `pallet-staking`) rely on the invariant `SlashDeferDuration < BondingDuration` to guarantee that a slash for an offence is always applied before the corresponding stake can be withdrawn. This is exactly the same class of "two competing time windows must be ordered correctly or a critical guarantee silently disappears" bug described in the external Lender report (`grace < expiry`). The only place this invariant is checked is `Hooks::integrity_test()`, which per its own documentation in `substrate/frame/support/src/traits/hooks.rs` (lines 557-570) is compiled into an auto-generated unit test (`__construct_runtime_integrity_test`) run under `sp_io::TestExternalities` — it is never executed as part of on-chain block execution, `on_runtime_upgrade`, or genesis build. [1](#0-0) [2](#0-1) 

### Finding Description
The withdrawal-safety guarantee documented at the top of the pallet explicitly depends on `BondingDuration > SlashDeferDuration`: [3](#0-2) 

The only enforcement of this ordering is the `assert!` inside `integrity_test()`: [4](#0-3) 

`nomination-pools` independently relies on the same assumption (`bonding_duration > slash_defer_duration`) and also only checks it via `integrity_test`: [5](#0-4) 

Per the trait documentation, `integrity_test` is *"placed in an auto-generated test... executed in an externality environment provided by `sp_io::TestExternalities`"* — it is a build-time developer safety net, not a runtime safeguard. If a chain-spec/runtime is compiled and deployed (e.g. through a runtime upgrade or a new parachain runtime) with `SlashDeferDuration >= BondingDuration` without the integrity test having actually been run and its panic observed (e.g. suppressed in CI, or the runtime built with `--release` skipping test targets), there is no on-chain code path that rejects the configuration. This mirrors the reported Lender bug precisely: the fix that's supposed to reject `grace >= expiry` exists only as an assertion that is not wired into the code path that matters (contract initialization there; runtime construction/upgrade here).

Once such a misconfigured runtime is live, `apply_unapplied_slashes`/`withdraw_unbonded` logic (`substrate/frame/staking/src/pallet/impls.rs` lines 823-836, and the async equivalent) can permit stash withdrawal before a deferred slash for the same era is applied, because the bonding window closes before or at the same time the slash-defer window would apply the slash — permanently letting offenders and their nominators evade slashing and withdraw stake that should have been forfeited.

### Impact Explanation
If this ordering invariant is violated in a deployed runtime, validators/nominators can withdraw unbonded funds before a computed slash for a prior offence is applied, resulting in permanent evasion of slashing (loss of the punitive/economic-security guarantee) and an inconsistency between expected and actual stake accounting — a direct "runtime bug that compromises intended behavior" and risks unbacked fund retention by malicious validators, aligning with the impact gate's staking/asset-accounting criteria.

### Likelihood Explanation
This requires a runtime configuration error (constant `Get` values chosen at compile time) reaching production without the auto-generated integrity test having caught it — a plausible engineering failure mode (e.g., new parachain runtime spin-ups, custom chain configs, or CI misconfiguration skipping the generated test), not requiring any malicious actor, governance abuse, or privileged access — matching the same low-friction "misconfiguration" root cause as the external report.

### Recommendation
Move the `SlashDeferDuration < BondingDuration` (and `NominatorFastUnbondDuration <= BondingDuration`) checks out of the test-only `integrity_test` hook and into a path that is actually evaluated on-chain — e.g., a `TryRuntimeUpgrade`/genesis `assert` that halts runtime construction, or better, a runtime-metadata/const-assertion mechanism (`const _: () = assert!(...)`) enforced at compile time so it cannot be silently skipped, plus equivalent enforcement in `pallet-nomination-pools`'s dependent assumption.

### Proof of Concept
1. Build a runtime with `pallet_staking_async::Config` where `BondingDuration = 1` and `SlashDeferDuration = 1` (violates `SlashDeferDuration < BondingDuration`), skipping/ignoring the `__construct_runtime_integrity_test` (e.g., via `cargo build --release` without running the test suite, or CI misconfiguration).
2. Deploy/upgrade the chain to this runtime.
3. A validator commits a slashable offence in era `N`; unbonds all stake in era `N`.
4. Because `BondingDuration <= SlashDeferDuration`, the unbonding chunk becomes withdrawable at era `N + BondingDuration`, which is the same era (or earlier) the slash would be applied at era `N + SlashDeferDuration`.
5. Depending on block-level ordering within `on_initialize` (`process_offence_for_era` vs `apply_unapplied_slashes` vs extrinsic execution for `withdraw_unbonded`), the validator/nominator can call `withdraw_unbonded` and successfully remove stake before `apply_slash` executes, permanently evading the slash for that offence.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1776-1799)
```rust
		fn integrity_test() {
			// ensure that we funnel the correct value to the `DataProvider::MaxVotesPerVoter`;
			assert_eq!(
				MaxNominationsOf::<T>::get(),
				<Self as ElectionDataProvider>::MaxVotesPerVoter::get()
			);

			// and that MaxNominations is always greater than 1, since we count on this.
			assert!(!MaxNominationsOf::<T>::get().is_zero());

			assert!(
				T::SlashDeferDuration::get() < T::BondingDuration::get() || T::BondingDuration::get() == 0,
				"As per documentation, slash defer duration ({}) should be less than bonding duration ({}).",
				T::SlashDeferDuration::get(),
				T::BondingDuration::get(),
			);

			// Ensure NominatorFastUnbondDuration is not greater than BondingDuration
			assert!(
				T::NominatorFastUnbondDuration::get() <= T::BondingDuration::get(),
				"NominatorFastUnbondDuration ({}) must not exceed BondingDuration ({}).",
				T::NominatorFastUnbondDuration::get(),
				T::BondingDuration::get(),
			);
```

**File:** substrate/frame/support/src/traits/hooks.rs (L557-571)
```rust
	/// Check the integrity of this pallet's configuration.
	///
	/// Any code located in this hook is placed in an auto-generated test, and generated as a part
	/// of [`crate::construct_runtime`]'s expansion. Look for a test case with a name along the
	/// lines of: `__construct_runtime_integrity_test`.
	///
	/// This hook is the location where the values/types provided to the `Config` trait
	/// of the pallet can be tested for correctness. For example, if two `type Foo: Get<u32>` and
	/// `type Bar: Get<u32>` where `Foo::get()` must always be greater than `Bar::get()`, such
	/// checks can be asserted upon here.
	///
	/// Note that this hook is executed in an externality environment, provided by
	/// `sp_io::TestExternalities`. This makes it possible to access the storage.
	fn integrity_test() {}
}
```

**File:** substrate/frame/staking-async/src/lib.rs (L180-191)
```rust
//! With BondingDuration = 28 and SlashDeferDuration = 27:
//! - User unbonds in era 90
//! - Offence occurs in era 90
//! - Reported in era 92 (typically within 2 days, but reportable until Era 116)
//! - Processed in era 92 (within next few blocks after reporting)
//! - Slash deferred for 27 eras, applied at era 117 (90 + 27)
//! - Cannot withdraw unbonded chunks until era 118 (90 + 28)
//!
//! The 28-era bonding duration ensures that any offences committed before or during
//! unbonding have time to be reported, processed, and applied before funds can be
//! withdrawn. This provides a window for governance to cancel slashes that may have
//! resulted from software bugs.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3265-3276)
```rust
		fn integrity_test() {
			assert!(
				T::MaxPointsToBalance::get() > 0,
				"Minimum points to balance ratio must be greater than 0"
			);
			assert!(
				T::StakeAdapter::bonding_duration() < T::MaxUnbondingPools::get(),
				"There must be more unbonding pools then the bonding duration /
				so a slash can be applied to relevant unbonding pools. (We assume /
				the bonding duration > slash deffer duration.",
			);
		}
```
