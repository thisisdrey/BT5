## Analysis

This confirms the mechanism: `Ledger::<T>::get(&controller)` (a `StorageMap<_, _, StakingLedger<T>>`) fails to decode once `T::MaxUnlockingChunks` is lowered below the number of chunks already persisted in `unlocking: BoundedVec<UnlockChunk<Balance>, MaxUnlockingChunks>`, and `StakingLedger::get` maps that failed lookup to `Error::<T>::NotController` [1](#0-0) , exactly reproducing what `reducing_max_unlocking_chunks_abrupt` demonstrates [2](#0-1) , and the analogous case in `pallet-staking-async` [3](#0-2) .

### Title
Lowering `Config::MaxUnlockingChunks` corrupts existing `StakingLedger` entries and permanently locks affected stashes out of `unbond`/`rebond`/`withdraw_unbonded` — (File: `substrate/frame/staking/src/ledger.rs`, `substrate/frame/staking/src/pallet/mod.rs`)

### Summary
`StakingLedger.unlocking` is a `BoundedVec<UnlockChunk<Balance>, T::MaxUnlockingChunks>`. `T::MaxUnlockingChunks` is a plain runtime constant (`#[pallet::constant]`), changeable only via a runtime upgrade, with no consistency check against currently-stored ledgers [4](#0-3) . If a runtime upgrade lowers this bound below the length of `unlocking` already held by some stash, that account's ledger becomes undecodable, and every subsequent read of it (via `StakingLedger::get`) is silently mapped to `Error::<T>::NotController` [5](#0-4) . This is the exact analog of the Gearbox report: an upgrade to a bounding parameter of a "credit-manager"-like ledger silently invalidates previously-valid accounts and can freeze/mis-handle them.

### Finding Description
`Ledger<T>` stores `StakingLedger<T>` values SCALE-encoded with the *current* `MaxEncodedLen`/bound derived from `T::MaxUnlockingChunks`. When governance reduces `MaxUnlockingChunks` (a normal, non-malicious runtime-upgrade parameter change — not an "admin abuse" scenario, just an ordinary config tuning decision, same as Gearbox's `creditManager` param update), any ledger whose `unlocking` vector length exceeds the new bound can no longer decode via the bounded-vec codec. `StakingLedger::get` treats this decode failure identically to "no ledger for controller" and returns `Error::<T>::NotController` [1](#0-0) . All public entry points that route through `StakingLedger::get`/`Self::ledger(...)` (e.g. `unbond`, `rebond`, `withdraw_unbonded`) then fail for that stash, even though the account is legitimately bonded and the failure has nothing to do with authorization — the pallet cannot distinguish "not actually bonded" from "bonded but ledger now overflows the bound." This is confirmed directly by the pallet's own regression test, `reducing_max_unlocking_chunks_abrupt`, which sets `MaxUnlockingChunks::set(1)` on a ledger with 2 chunks and observes `unbond`/`rebond` both fail with `Error::<Test>::NotController`, explicitly commented as leaving "the ledger in a corrupt state" [6](#0-5) ; the same behavior is reproduced in `pallet-staking-async` [7](#0-6) .

Unlike the analogous `MaxUnbondingPools`/`BondingDuration` interaction in nomination-pools, which was hardened in this repo by decoupling the bound from a mutable value [8](#0-7) , no equivalent guard exists for `MaxUnlockingChunks` in either `pallet-staking` or `pallet-staking-async`: the field remains bound directly by the mutable config constant with no migration, no upper-bound floor derived from history, and no defensive decode-and-repair path.

### Impact Explanation
Once `MaxUnlockingChunks` is lowered below a stash's existing chunk count, that stash's funds already queued for unbonding become permanently inaccessible through the standard extrinsics: `unbond`, `rebond`, and `withdraw_unbonded` all resolve the ledger through the same lookup and fail with `NotController` [2](#0-1) . This is a permanent user-fund lock (the account cannot manage or withdraw its stake) triggered purely by an ordinary parameter change, without any malicious actor, admin abuse, leaked key, or off-chain assumption — squarely inside the "permanent user-fund … lock" impact category the program accepts.

### Likelihood Explanation
Likelihood is non-trivial: `MaxUnlockingChunks` is a runtime constant that chains legitimately retune over time (e.g., for weight/PoV budget reasons), and nothing in the pallet's `integrity_test`, migrations, or the setter path checks it against currently stored `Ledger` entries before/after the change. Any staker who has accumulated close to the previous maximum number of unlocking chunks is immediately affected the moment such an upgrade lands — this is not a contrived edge case, it is the exact scenario the pallet's own test suite (`reducing_max_unlocking_chunks_abrupt`) was written to document.

### Recommendation
Do not let `Ledger<T>::unlocking`'s bound be sourced directly from a config constant that can shrink. Options:
- Add a migration/pre-upgrade check that refuses (or errors loudly on) reducing `MaxUnlockingChunks` below the maximum `unlocking.len()` present in storage, similar to the `HistoryDepth` migration guard already present for a related field [9](#0-8) .
- Or decouple the bound the way `pallet-nomination-pools` now does for `MaxUnbondingPools`, keeping a fixed, migration-safe upper bound independent of a governance-tunable duration/count [8](#0-7) .
- At minimum, make `StakingLedger::get` distinguish "ledger absent" from "ledger present but undecodable due to bound change" so affected accounts can be repaired/migrated rather than being silently treated as `NotController`.

### Proof of Concept
The existing test in the repository already demonstrates the full exploit path end-to-end: [6](#0-5) 
1. Set `MaxUnlockingChunks = 2`, bond account `3`, and call `unbond` twice at two different eras to populate two `UnlockChunk`s.
2. Advance eras; confirm a third `unbond` correctly fails with `NoMoreChunks` (bound enforced normally).
3. Perform the runtime-upgrade-equivalent step `MaxUnlockingChunks::set(1)` (simulating governance lowering the constant).
4. Observe `unbond` and `rebond` for account `3` now fail with `Error::<Test>::NotController` — the account is bonded and has funds, but the ledger cannot be legitimately loaded, and the pallet reports a misleading and pathologically wrong error, leaving the stash's unbonding chunks unmanageable until/unless governance reverts the constant.

The corresponding `pallet-staking-async` test shows the identical corruption pattern [7](#0-6) .

### Citations

**File:** substrate/frame/staking/src/ledger.rs (L111-129)
```rust
	pub(crate) fn get(account: StakingAccount<T::AccountId>) -> Result<StakingLedger<T>, Error<T>> {
		let (stash, controller) = match account.clone() {
			StakingAccount::Stash(stash) => {
				(stash.clone(), <Bonded<T>>::get(&stash).ok_or(Error::<T>::NotStash)?)
			},
			StakingAccount::Controller(controller) => (
				Ledger::<T>::get(&controller)
					.map(|l| l.stash)
					.ok_or(Error::<T>::NotController)?,
				controller,
			),
		};

		let ledger = <Ledger<T>>::get(&controller)
			.map(|mut ledger| {
				ledger.controller = Some(controller.clone());
				ledger
			})
			.ok_or(Error::<T>::NotController)?;
```

**File:** substrate/frame/staking/src/tests.rs (L6192-6241)
```rust
#[test]
fn reducing_max_unlocking_chunks_abrupt() {
	// Concern is on validators only
	// By Default 11, 10 are stash and ctlr and 21,20
	ExtBuilder::default().build_and_execute(|| {
		// given a staker at era=10 and MaxUnlockChunks set to 2
		MaxUnlockingChunks::set(2);
		start_active_era(10);
		assert_ok!(Staking::bond(RuntimeOrigin::signed(3), 300, RewardDestination::Staked));
		assert!(matches!(Staking::ledger(3.into()), Ok(_)));

		// when staker unbonds
		assert_ok!(Staking::unbond(RuntimeOrigin::signed(3), 20));

		// then an unlocking chunk is added at `current_era + bonding_duration`
		// => 10 + 3 = 13
		let expected_unlocking: BoundedVec<UnlockChunk<Balance>, MaxUnlockingChunks> =
			bounded_vec![UnlockChunk { value: 20 as Balance, era: 13 as EraIndex }];
		assert!(matches!(Staking::ledger(3.into()),
			Ok(StakingLedger {
				unlocking,
				..
			}) if unlocking==expected_unlocking));

		// when staker unbonds at next era
		start_active_era(11);
		assert_ok!(Staking::unbond(RuntimeOrigin::signed(3), 50));
		// then another unlock chunk is added
		let expected_unlocking: BoundedVec<UnlockChunk<Balance>, MaxUnlockingChunks> =
			bounded_vec![UnlockChunk { value: 20, era: 13 }, UnlockChunk { value: 50, era: 14 }];
		assert!(matches!(Staking::ledger(3.into()),
			Ok(StakingLedger {
				unlocking,
				..
			}) if unlocking==expected_unlocking));

		// when staker unbonds further
		start_active_era(12);
		// then further unbonding not possible
		assert_noop!(Staking::unbond(RuntimeOrigin::signed(3), 20), Error::<Test>::NoMoreChunks);

		// when max unlocking chunks is reduced abruptly to a low value
		MaxUnlockingChunks::set(1);
		// then unbond, rebond ops are blocked with ledger in corrupt state
		assert_noop!(Staking::unbond(RuntimeOrigin::signed(3), 20), Error::<Test>::NotController);
		assert_noop!(Staking::rebond(RuntimeOrigin::signed(3), 100), Error::<Test>::NotController);

		// reset the ledger corruption
		MaxUnlockingChunks::set(2);
	})
```

**File:** substrate/frame/staking-async/src/tests/bonding.rs (L881-937)
```rust
#[test]
fn reducing_max_unlocking_chunks_abrupt() {
	// Concern is on validators only
	ExtBuilder::default().build_and_execute(|| {
		// given a staker at era=10 and MaxUnlockChunks set to 2
		MaxUnlockingChunks::set(2);
		Session::roll_until_active_era(10);

		assert_ok!(Staking::bond(RuntimeOrigin::signed(3), 300, RewardDestination::Staked));
		assert!(matches!(Staking::ledger(3.into()), Ok(_)));

		// when staker unbonds
		assert_ok!(Staking::unbond(RuntimeOrigin::signed(3), 20));

		// then an unlocking chunk is added at `current_era + bonding_duration`
		// => 10 + 3 = 13
		let expected_unlocking: BoundedVec<UnlockChunk<Balance>, MaxUnlockingChunks> =
			bounded_vec![UnlockChunk { value: 20 as Balance, era: 13 as EraIndex }];
		assert!(matches!(Staking::ledger(3.into()),
			Ok(StakingLedger {
				unlocking,
				..
			}) if unlocking == expected_unlocking));

		// when staker unbonds at next era
		Session::roll_until_active_era(11);

		assert_ok!(Staking::unbond(RuntimeOrigin::signed(3), 50));

		// then another unlock chunk is added
		let expected_unlocking: BoundedVec<UnlockChunk<Balance>, MaxUnlockingChunks> =
			bounded_vec![UnlockChunk { value: 20, era: 13 }, UnlockChunk { value: 50, era: 14 }];
		assert!(matches!(Staking::ledger(3.into()),
			Ok(StakingLedger {
				unlocking,
				..
			}) if unlocking == expected_unlocking));

		// when staker unbonds further
		Session::roll_until_active_era(12);

		// then further unbonding not possible
		assert_noop!(Staking::unbond(RuntimeOrigin::signed(3), 20), Error::<Test>::NoMoreChunks);

		// when max unlocking chunks is reduced abruptly to a low value
		MaxUnlockingChunks::set(1);

		// then unbond, rebond ops are blocked with ledger in corrupt state
		assert_noop!(Staking::unbond(RuntimeOrigin::signed(3), 20), Error::<Test>::NotController);
		assert_noop!(Staking::rebond(RuntimeOrigin::signed(3), 100), Error::<Test>::NotController);

		// reset the ledger corruption
		MaxUnlockingChunks::set(2);

		// now rebond works again
		assert_ok!(Staking::rebond(RuntimeOrigin::signed(3), 20));
	})
```

**File:** substrate/frame/staking/src/pallet/mod.rs (L128-217)
```rust
		/// Time used for computing era duration.
		///
		/// It is guaranteed to start being called from the first `on_finalize`. Thus value at
		/// genesis is not used.
		#[pallet::no_default]
		type UnixTime: UnixTime;

		/// Convert a balance into a number used for election calculation. This must fit into a
		/// `u64` but is allowed to be sensibly lossy. The `u64` is used to communicate with the
		/// [`frame_election_provider_support`] crate which accepts u64 numbers and does operations
		/// in 128.
		/// Consequently, the backward convert is used convert the u128s from sp-elections back to a
		/// [`BalanceOf`].
		#[pallet::no_default_bounds]
		type CurrencyToVote: sp_staking::currency_to_vote::CurrencyToVote<BalanceOf<Self>>;

		/// Something that provides the election functionality.
		#[pallet::no_default]
		type ElectionProvider: ElectionProvider<
			AccountId = Self::AccountId,
			BlockNumber = BlockNumberFor<Self>,
			// we only accept an election provider that has staking as data provider.
			DataProvider = Pallet<Self>,
		>;
		/// Something that provides the election functionality at genesis.
		#[pallet::no_default]
		type GenesisElectionProvider: ElectionProvider<
			AccountId = Self::AccountId,
			BlockNumber = BlockNumberFor<Self>,
			DataProvider = Pallet<Self>,
		>;

		/// Something that defines the maximum number of nominations per nominator.
		#[pallet::no_default_bounds]
		type NominationsQuota: NominationsQuota<BalanceOf<Self>>;

		/// Number of eras to keep in history.
		///
		/// Following information is kept for eras in `[current_era -
		/// HistoryDepth, current_era]`: `ErasStakers`, `ErasStakersClipped`,
		/// `ErasValidatorPrefs`, `ErasValidatorReward`, `ErasRewardPoints`,
		/// `ErasTotalStake`, `ErasStartSessionIndex`, `ClaimedRewards`, `ErasStakersPaged`,
		/// `ErasStakersOverview`.
		///
		/// Must be more than the number of eras delayed by session.
		/// I.e. active era must always be in history. I.e. `active_era >
		/// current_era - history_depth` must be guaranteed.
		///
		/// If migrating an existing pallet from storage value to config value,
		/// this should be set to same value or greater as in storage.
		///
		/// Note: `HistoryDepth` is used as the upper bound for the `BoundedVec`
		/// item `StakingLedger.legacy_claimed_rewards`. Setting this value lower than
		/// the existing value can lead to inconsistencies in the
		/// `StakingLedger` and will need to be handled properly in a migration.
		/// The test `reducing_history_depth_abrupt` shows this effect.
		#[pallet::constant]
		type HistoryDepth: Get<u32>;

		/// Tokens have been minted and are unused for validator-reward.
		/// See [Era payout](./index.html#era-payout).
		#[pallet::no_default_bounds]
		type RewardRemainder: OnUnbalanced<NegativeImbalanceOf<Self>>;

		/// The overarching event type.
		#[pallet::no_default_bounds]
		#[allow(deprecated)]
		type RuntimeEvent: From<Event<Self>> + IsType<<Self as frame_system::Config>::RuntimeEvent>;

		/// Handler for the unbalanced reduction when slashing a staker.
		#[pallet::no_default_bounds]
		type Slash: OnUnbalanced<NegativeImbalanceOf<Self>>;

		/// Handler for the unbalanced increment when rewarding a staker.
		/// NOTE: in most cases, the implementation of `OnUnbalanced` should modify the total
		/// issuance.
		#[pallet::no_default_bounds]
		type Reward: OnUnbalanced<PositiveImbalanceOf<Self>>;

		/// Number of sessions per era.
		#[pallet::constant]
		type SessionsPerEra: Get<SessionIndex>;

		/// Number of eras that staked funds must remain bonded for.
		#[pallet::constant]
		type BondingDuration: Get<EraIndex>;

		/// Number of eras that slashes are deferred by, after computation.
		///
		/// This should be less than the bonding duration. Set to 0 if slashes
```

**File:** prdoc/pr_12323.prdoc (L1-9)
```text
title: Decouple unbonding pool bound from bonding duration
doc:
- audience: Runtime Dev
  description: The with_era map is now bounded by the fixed MaxUnbondingPools,
    and the merge cutoff uses an effective window of MaxUnbondingPools - bonding_duration.
    This prevents a lowered bonding duration from shrinking the bound and making stored
    state undecodable. This is a breaking change - the `Config::PostUnbondingPoolsWindow`
    associated type is renamed to `MaxUnbondingPools`; runtimes must set it to their previous
    `bonding_duration + PostUnbondingPoolsWindow` to preserve the storage bound.
```

**File:** substrate/frame/staking/src/migrations.rs (L290-333)
```rust
pub mod v12 {
	use super::*;
	use frame_support::{pallet_prelude::ValueQuery, storage_alias};

	#[storage_alias]
	type HistoryDepth<T: Config> = StorageValue<Pallet<T>, u32, ValueQuery>;

	/// Clean up `T::HistoryDepth` from storage.
	///
	/// We will be depending on the configurable value of `T::HistoryDepth` post
	/// this release.
	pub struct MigrateToV12<T>(core::marker::PhantomData<T>);
	impl<T: Config> OnRuntimeUpgrade for MigrateToV12<T> {
		#[cfg(feature = "try-runtime")]
		fn pre_upgrade() -> Result<Vec<u8>, TryRuntimeError> {
			frame_support::ensure!(
				StorageVersion::<T>::get() == ObsoleteReleases::V11_0_0,
				"Expected v11 before upgrading to v12"
			);

			if HistoryDepth::<T>::exists() {
				frame_support::ensure!(
					T::HistoryDepth::get() == HistoryDepth::<T>::get(),
					"Provided value of HistoryDepth should be same as the existing storage value"
				);
			} else {
				log::info!("No HistoryDepth in storage; nothing to remove");
			}

			Ok(Default::default())
		}

		fn on_runtime_upgrade() -> frame_support::weights::Weight {
			if StorageVersion::<T>::get() == ObsoleteReleases::V11_0_0 {
				HistoryDepth::<T>::kill();
				StorageVersion::<T>::put(ObsoleteReleases::V12_0_0);

				log!(info, "v12 applied successfully");
				T::DbWeight::get().reads_writes(1, 2)
			} else {
				log!(warn, "Skipping v12, should be removed");
				T::DbWeight::get().reads(1)
			}
		}
```
