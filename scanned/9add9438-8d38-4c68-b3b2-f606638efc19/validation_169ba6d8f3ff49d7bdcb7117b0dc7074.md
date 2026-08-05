### Title
Era reward pot slot reused via `era % POT_POOL_SIZE` without a live check that `HistoryDepth` still fits the pool — stale/mismatched pot reuse can misroute or duplicate reward payouts - ([File: substrate/frame/staking-async/src/reward.rs])

### Summary
`pallet-staking-async` reuses a fixed pool of `POT_POOL_SIZE = 200` reward-pot accounts across eras, indexed by `era % POT_POOL_SIZE`, instead of a fresh account per era. Reuse safety depends entirely on a compile-time/test-time invariant (`POT_POOL_SIZE > HistoryDepth`) checked only by an `integrity_test`, never re-validated when a pot slot is actually reused at era-rotation time. This is structurally the same bug class as the `UnstakeCooldown` proxy-pool report: a pooled resource is handed back for reuse based only on "is it free/idle" (providers == 0, or drained balance) rather than "does its current binding still match the caller's live configuration."

### Finding Description
The pool is defined and documented as: [1](#0-0) 

Pot creation is idempotent purely on provider-count, with no check against the era that last occupied the slot: [2](#0-1) 

Cleanup drains the balance but deliberately keeps the account "alive" for the next era sharing the same slot: [3](#0-2) 

The only thing preventing two live eras (i.e., eras still inside `HistoryDepth`, hence still payable/claimable) from colliding on the same slot is the relation `POT_POOL_SIZE > HistoryDepth`, enforced by an `integrity_test` (per the doc comment in `lib.rs` and the PR description): [4](#0-3) 

`integrity_test` in FRAME pallets is a build/genesis-time (or `try-runtime`) check — it is not re-evaluated on every runtime upgrade. If `Config::HistoryDepth` is later raised via a routine runtime-parameter change (not necessarily malicious admin abuse — just a legitimate governance-set `Get` constant that nobody re-validated against `POT_POOL_SIZE`) to a value `>= 200`, `create()`/`drain()`/`snapshot_era_rewards()` will happily let a new era's `pot_slot(era)` collide with an older era that is still within the (now larger) history/payable window. `create()` only checks `providers(&pot_account) == 0`; it never checks "which era currently owns this slot," so a stale era's still-unclaimed/still-payable pot can be silently reused, snapshotted-into, and drained by a completely different era.

This mirrors the reported bug precisely: the pool hands back a reusable resource (proxy / pot account) purely based on an availability signal (proxy popped from queue / provider count zero) rather than validating that the resource's binding (implementation / owning era) is still consistent with the caller's current expectations (implementations[token] / HistoryDepth window).

### Impact Explanation
If a slot collision occurs while an older era is still within its payable window, staker rewards from one era can be commingled with, overwritten by, or drained alongside another era's rewards. This directly implicates "duplicate settlement or payout" and "theft or unbacked mint or unlock" categories in-scope for the program: rewards belonging to era A's stakers could end up paid to/mixed with era B's stakers, or be zeroed out by `drain()` before the rightful era's claimants can withdraw them — a fund-loss/misrouting condition on live reward accounting.

### Likelihood Explanation
The `integrity_test` was clearly written by the authors specifically because they recognized this exact failure mode ("Must be strictly greater than `Config::HistoryDepth` so that a slot is only reused after its previous era has been pruned and drained"), which confirms the invariant is real and load-bearing. However, nothing in `create()`, `drain()`, or `snapshot_era_rewards()` re-asserts this invariant at the point of reuse — the protection exists only as a one-time, off-path test assertion. Any future config change (a normal, expected kind of runtime parameter tuning, not privileged "admin abuse" of an exploit) that violates `POT_POOL_SIZE > HistoryDepth` silently reintroduces slot collisions with no runtime-level guard to catch it.

### Recommendation
Do not rely solely on a build-time `integrity_test` for a live safety invariant. At the point of `create()`/`snapshot_era_rewards()`, track which era currently owns a given slot (e.g., store `SlotOwner: Map<u32 /* slot */, EraIndex>`) and assert/guard that the slot is not being reused while the previous owning era is still within `HistoryDepth` (unpaid/unclaimed). Fail closed (defensive/e.g. `ensure!`) rather than silently overwriting, and consider making the `POT_POOL_SIZE > HistoryDepth` relationship enforced dynamically (e.g., via `on_runtime_upgrade` validation for `HistoryDepth` changes) rather than only via `integrity_test`.

### Proof of Concept
1. Chain runs with `POT_POOL_SIZE = 200` and `HistoryDepth = 84` (invariant holds: `200 > 84`).
2. Governance later raises `HistoryDepth` to `250` via a routine parameter-update runtime upgrade (no `integrity_test` re-run on-chain).
3. Era `E` creates/funds pot slot `E % 200`. Because `HistoryDepth = 250 > POT_POOL_SIZE = 200`, era `E` is still within the payable/history window when era `E + 200` arrives.
4. Era `E + 200` calls `EraRewardManager::create`/`snapshot_era_rewards`, computing the same slot (`(E+200) % 200 == E % 200`); `create()` sees `providers > 0` and treats the slot as already-initialized, then `snapshot_era_rewards` transfers new inflation into the same account still holding era `E`'s unclaimed balance.
5. Result: era `E`'s stakers' unclaimed rewards are commingled with/overwritten by era `E+200`'s rewards, or a subsequent `cleanup_era`/`drain()` for either era wipes funds still owed to the other era's claimants — a duplicate/misrouted payout with no runtime check having fired, exactly as in the external report's proxy-implementation-mismatch scenario. [5](#0-4)

### Citations

**File:** substrate/frame/staking-async/src/lib.rs (L585-600)
```rust
/// Size of the rotating pool of era-specific pot accounts.
///
/// Era pots are addressed by `era % POT_POOL_SIZE`, so a pot account is reused
/// every `POT_POOL_SIZE` eras instead of a fresh account being created per era.
/// This bounds the total storage footprint contributed by era pot accounts to a
/// constant rather than growing with chain age.
///
/// Must be strictly greater than [`Config::HistoryDepth`] so that a slot is only
/// reused after its previous era has been pruned and drained. The
/// [`integrity_test`] enforces this invariant at runtime startup.
pub(crate) const POT_POOL_SIZE: u32 = 200;

/// Maps an era index to its slot in the rotating pot pool.
pub(crate) fn pot_slot(era: EraIndex) -> u32 {
	era % POT_POOL_SIZE
}
```

**File:** substrate/frame/staking-async/src/reward.rs (L66-82)
```rust
impl<T: Config> EraRewardManager<T> {
	/// Ensures the era pot account for `(era, kind)` exists by holding a provider
	/// reference. Idempotent: if the slot's account is already provided (because a
	/// previous era reused it), this is a no-op.
	///
	/// Should only be called in non-minting mode (`DisableMinting = true`).
	pub(crate) fn create(era: EraIndex, kind: RewardKind) -> T::AccountId {
		debug_assert!(
			T::DisableMinting::get(),
			"Era pots should only be created when DisableMinting is true"
		);
		let pot_account = T::RewardPots::pot_account(RewardPot::Era(era, kind));
		if frame_system::Pallet::<T>::providers(&pot_account) == 0 {
			frame_system::Pallet::<T>::inc_providers(&pot_account);
		}
		pot_account
	}
```

**File:** substrate/frame/staking-async/src/reward.rs (L156-222)
```rust
	/// Drains an era pot's remaining balance to the unclaimed reward handler.
	///
	/// The pot account itself is kept alive (provider retained) so the same slot
	/// can be reused by a future era. No-op if the pot was never created (e.g.
	/// the era ran in legacy minting mode).
	pub(crate) fn drain(era: EraIndex, kind: RewardKind) {
		let pot_account = T::RewardPots::pot_account(RewardPot::Era(era, kind));

		// Skip if pot was never created (legacy mode doesn't create pots).
		if frame_system::Pallet::<T>::providers(&pot_account) == 0 {
			return;
		}

		let remaining = T::Currency::balance(&pot_account);

		if remaining.is_zero() {
			return;
		}

		match T::Currency::withdraw(
			&pot_account,
			remaining,
			Precision::BestEffort,
			Preservation::Expendable,
			Fortitude::Force,
		) {
			Ok(credit) => {
				T::UnclaimedRewardHandler::on_unbalanced(credit);
				log!(
					debug,
					"Drained {:?} unclaimed rewards from era {:?} {:?} pot",
					remaining,
					era,
					kind
				);
			},
			Err(e) => {
				defensive!("Failed to withdraw unclaimed rewards from era pot");
				log!(
					error,
					"Era {:?} {:?}: unclaimed reward withdrawal failed: {:?}",
					era,
					kind,
					e
				);
			},
		}
	}

	/// Whether the slot backing this era's staker reward pot exists.
	///
	/// Because slots are reused across eras (rotating pool), this returns
	/// `true` for an era as long as *some* era mapping to the same slot
	/// has created the account.
	#[cfg(any(test, feature = "try-runtime"))]
	pub(crate) fn has_staker_rewards_pot(era: EraIndex) -> bool {
		let pot = T::RewardPots::pot_account(RewardPot::Era(era, RewardKind::StakerRewards));
		frame_system::Pallet::<T>::providers(&pot) > 0
	}

	/// Cleans up all pot accounts for a given era by draining their balances.
	///
	/// Pot accounts are kept alive for reuse by a future era at the same slot.
	pub(crate) fn cleanup_era(era: EraIndex) {
		Self::drain(era, RewardKind::StakerRewards);
		Self::drain(era, RewardKind::ValidatorSelfStake);
	}
```

**File:** prdoc/stable2606/pr_11930.prdoc (L1-16)
```text
title: 'pallet-staking-async: Rotate era reward pots through a fixed-size pool'
doc:
- audience: Runtime Dev
  description: |-
    Era reward pot accounts are now drawn from a fixed pool of `POT_POOL_SIZE = 200`
    accounts, indexed by `era % POT_POOL_SIZE`, instead of one fresh account per era.
    This ensure we only use a fixed size of pot accounts for the lifetime of the 
    chain rather than growing per era.

    An `integrity_test` enforces `POT_POOL_SIZE > HistoryDepth` so a slot is only
    reused after its previous era has been pruned.
crates:
- name: pallet-staking-async
  bump: minor
- name: asset-hub-westend-runtime
  bump: minor
```

**File:** substrate/frame/staking-async/src/tests/era_rotation.rs (L565-601)
```rust
#[test]
fn pot_slot_reuse_drain_then_recreate_is_idempotent() {
	// Drain must keep the slot alive, and a subsequent `create()` on a future
	// era sharing the same slot must not double-increment the provider.
	ExtBuilder::default().build_and_execute(|| {
		let era_a = 5;
		let era_b = era_a + POT_POOL_SIZE;

		// GIVEN: era_a's pot is created and funded.
		let pot = EraRewardManager::<Test>::create(era_a, RewardKind::StakerRewards);
		assert_eq!(System::providers(&pot), 1);
		let funded: Balance = 1_000;
		Balances::set_balance(&pot, funded);
		assert_eq!(Balances::balance(&pot), funded);

		// WHEN: era_a's pot is cleaned up past HistoryDepth.
		EraRewardManager::<Test>::cleanup_era(era_a);

		// THEN: balance drained, provider retained (slot kept alive).
		assert_eq!(Balances::balance(&pot), 0);
		assert_eq!(System::providers(&pot), 1, "drain must not release the provider");

		// WHEN: era_b reuses the same slot.
		EraRewardManager::<Test>::create(era_b, RewardKind::StakerRewards);

		// THEN: provider count unchanged (idempotent create).
		assert_eq!(
			System::providers(&pot),
			1,
			"create must not double-increment provider on slot reuse"
		);

		// AND: a fresh snapshot into the reused slot works as if it were new.
		Balances::set_balance(&pot, 2_000);
		assert_eq!(Balances::balance(&pot), 2_000);
	});
}
```
