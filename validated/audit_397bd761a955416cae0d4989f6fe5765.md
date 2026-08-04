## Title
Repurposed `AllowedRelayParents` storage key across `pallet-parachains-shared` V1→V2 causes relay-parent proof data corruption on incomplete/rolled-back migration - (File: `polkadot/runtime/parachains/src/shared/migration.rs`)

### Summary
The `parachains-shared` pallet exhibits the same storage-layout hazard described in the external report: a storage item is renamed in place with an incompatible type across an upgrade, and nothing in the runtime prevents the old-format reader code from ever being executed again against the new-format bytes. In `polkadot/runtime/parachains/src/shared/migration.rs`, `AllowedRelayParents` is a `StorageValue<AllowedRelayParentsTracker<Hash, BlockNumber>>` in V1, then the **same storage name/key** is reused in V2 as a `StorageDoubleMap<SessionIndex, Hash, RelayParentInfo>` [1](#0-0) [2](#0-1) . The migration comment itself flags this: "In v2, this storage name is reused as a StorageDoubleMap, so the old value must be removed" [3](#0-2) .

### Finding Description
`MigrateToV2` is only guarded by `VersionedMigration<1, 2, ...>`, i.e. it only fires if `on_chain_storage_version() == 1` [4](#0-3) . This is the exact analog of the LiquidityWindow situation: the guard only protects the forward path. Nothing in the pallet or in `frame_support`'s `VersionedMigration`/`OnRuntimeUpgrade` machinery prevents the on-chain storage version from being set back to `1` (or the runtime binary being downgraded to a pre-V2 build) while the underlying key still contains V2-encoded `StorageDoubleMap` bytes.

If that occurs, the old V1 code path (`v1::AllowedRelayParents::<T>::get()` as a `StorageValue<AllowedRelayParentsTracker<...>>`) will attempt to SCALE-decode the double-map's key-prefixed entries as a single `AllowedRelayParentsTracker` value. Because `ValueQuery` is used on the V1 alias, a decode failure silently falls back to `Default::default()` (empty tracker) rather than erroring [5](#0-4) . This is functionally identical to the reported bug class: reordered/repurposed slots being reinterpreted under the wrong layout, producing either garbage or silently-empty state instead of a decode error that would halt execution.

`AllowedRelayParents`/`AllowedSchedulingParents` are not decorative bookkeeping — they are the runtime's proof-binding state for relay-parent legitimacy used during backing/inclusion: `Pallet::<T>::get_relay_parent_info` reads the double map to validate that a backed candidate's declared relay parent is one the runtime actually observed [6](#0-5) , and this is consumed from `paras_inherent`/`inclusion` when verifying backed candidates. `AllowedSchedulingParentsTracker::acquire_info` similarly binds which scheduling parent + claim queue a candidate's backing group commitment is checked against [7](#0-6) . If this data set is unexpectedly emptied or corrupted, the check binding "relay-parent hash → correct state root / claim queue for this exact block" no longer reflects reality — precisely the invariant the Required Impact Gate calls out: proofs must bind chain, route, authority set, nonce, payload "exactly once."

### Impact Explanation
A corruption or unexpected reset of `AllowedRelayParents`/`AllowedSchedulingParents` breaks the binding between a backed candidate's claimed relay parent and the state the chain actually observed at that block. Depending on which direction the corruption goes:
- If the tracker/double-map is wiped (as V1's `ValueQuery` default would produce on failed decode), legitimate candidates could be rejected chain-wide, stalling parachain block inclusion/backing — a public underpriced-work/availability degradation of block production.
- If stale V1-shaped data lingers and is misread as V2 entries (or vice versa), a candidate could be validated against a `state_root`/`claim_queue`/block-number combination that does not correspond to the real relay-parent it claims, which is the "forged or mis-bound proof acceptance" class explicitly named in scope.

This is a runtime-wide (not application-level) storage integrity issue in a core parachains pallet, so its blast radius covers Polkadot/Kusama and every parachain relying on `pallet-parachains-shared` relay-parent tracking for backing/inclusion.

### Likelihood Explanation
Likelihood is Low-Medium and structurally identical to the disclosed analog: it requires the storage version to move backward relative to the code (a downgrade/incomplete-forward-only-migration scenario), which in Substrate happens through a runtime code change (`set_code`) — the same class of trigger as the "rollback to V1" runbook operation in the original report. No malicious peer/validator/collator is needed; the only actor involved is whoever authors/ships the next runtime upgrade, and the danger is that **nothing technical stops it** — there is no assertion anywhere that on-chain storage version cannot regress, and the `VersionedMigration` guard only checks equality to `FROM`, silently no-op-ing (with only a `log::warn!`) rather than refusing the upgrade if the version is already past `TO` [8](#0-7) . This mirrors exactly why the original finding was rated Medium: the danger isn't that it happens every day, it's that the system provides no structural guard against it and the failure mode is silent (empty state / wrong data) rather than a loud decode panic.

### Recommendation
- Add an explicit assertion in `shared` pallet's `on_runtime_upgrade`/`try-runtime` checks (and ideally in `VersionedMigration` generally) that on-chain storage version can never regress below the in-code version, causing a hard failure rather than silent reinterpretation.
- For `AllowedRelayParents` specifically, do not reuse the storage item name across incompatible types between V1 and V2; use a distinct storage key for the V2 double map so an accidental downgrade produces a clean "key not found" instead of type-confused decoding.
- Avoid `ValueQuery` defaults on migration-sensitive aliases where a decode failure should be surfaced (`OptionQuery` + explicit handling) instead of silently returning `Default::default()`.
- Document explicitly (as was done for the reported bug) that runtime downgrades below the version that performed this migration are unsafe and unsupported without a dedicated reverse migration.

### Proof of Concept
Conceptual repro (cannot be executed here, but structurally verifiable from the code):
1. Runtime is at `shared` pallet storage version 2; `AllowedRelayParents::<T>` (double map) contains real session-keyed relay-parent entries used by `get_relay_parent_info`.
2. A subsequent runtime upgrade (via `set_code`) ships a build whose `shared` pallet code is the pre-V2 version (i.e., reads `AllowedRelayParents` as the V1 `StorageValue<AllowedRelayParentsTracker>`), and/or `StorageVersion` is written back to `1` (no code exists today preventing this write).
3. On the next block, code calling `v1::AllowedRelayParents::<T>::get()` decodes the double map's raw bytes under `Decode` for `AllowedRelayParentsTracker` at the same key prefix. Because the alias is `ValueQuery`, any decode failure yields `Default::default()` — an empty tracker — silently discarding all currently-tracked relay-parent legitimacy data [5](#0-4) .
4. `Pallet::<T>::get_relay_parent_info` and `AllowedSchedulingParentsTracker::acquire_info`, which both gate backing/inclusion validity checks against this state [6](#0-5) [7](#0-6) , now operate on an inconsistent/empty view, causing wholesale rejection (or, depending on the exact byte layout collision, mis-acceptance) of backed candidates network-wide.

This cannot be demonstrated end-to-end without controlling a runtime upgrade path in a live/test node (out of scope for static analysis), so the concrete decode-collision byte-level behavior (empty vs. garbage vs. panic) is unverified here and would need to be confirmed via a `try-runtime` fork test that performs the downgrade path.

### Citations

**File:** polkadot/runtime/parachains/src/shared/migration.rs (L29-37)
```rust
	/// The old `AllowedRelayParents` storage at version 1 (a StorageValue).
	/// This occupied the storage key `twox128("ParasShared") ++ twox128("AllowedRelayParents")`.
	/// In v2, this storage name is reused as a StorageDoubleMap, so the old value must be removed.
	#[storage_alias]
	pub(crate) type AllowedRelayParents<T: Config> = StorageValue<
		Pallet<T>,
		AllowedRelayParentsTracker<<T as frame_system::Config>::Hash, BlockNumberFor<T>>,
		ValueQuery,
	>;
```

**File:** polkadot/runtime/parachains/src/shared/migration.rs (L227-234)
```rust
/// Migrate shared module storage from v1 to v2.
pub type MigrateToV2<T> = frame_support::migrations::VersionedMigration<
	1,
	2,
	v2::VersionUncheckedMigrateToV2<T>,
	Pallet<T>,
	<T as frame_system::Config>::DbWeight,
>;
```

**File:** polkadot/runtime/parachains/src/shared.rs (L108-122)
```rust
	/// Attempt to acquire the state root and block number to be used when building
	/// upon the given scheduling-parent.
	pub(crate) fn acquire_info(
		&self,
		scheduling_parent: Hash,
	) -> Option<(&SchedulingParentInfo<Hash>, BlockNumber)> {
		let pos = self
			.buffer
			.iter()
			.position(|info| info.scheduling_parent == scheduling_parent)?;
		let age = (self.buffer.len() - 1) - pos;
		let number = self.latest_number - BlockNumber::from(age as u32);

		Some((&self.buffer[pos], number))
	}
```

**File:** polkadot/runtime/parachains/src/shared.rs (L160-169)
```rust
	/// All allowed relay parents, keyed by (session_index, relay_parent_hash).
	#[pallet::storage]
	pub(crate) type AllowedRelayParents<T: Config> = StorageDoubleMap<
		_,
		Twox64Concat,
		SessionIndex,
		Blake2_128Concat,
		T::Hash,
		RelayParentInfo<T::Hash, BlockNumberFor<T>>,
	>;
```

**File:** polkadot/runtime/parachains/src/shared.rs (L313-319)
```rust
	/// Retrieve relay parent info by session index and relay parent hash.
	pub fn get_relay_parent_info(
		session_index: SessionIndex,
		relay_parent: T::Hash,
	) -> Option<RelayParentInfo<T::Hash, BlockNumberFor<T>>> {
		AllowedRelayParents::<T>::get(session_index, relay_parent)
	}
```

**File:** substrate/frame/support/src/migrations.rs (L144-171)
```rust
	fn on_runtime_upgrade() -> Weight {
		let on_chain_version = Pallet::on_chain_storage_version();
		if on_chain_version == FROM {
			log::info!(
				"🚚 Pallet {:?} VersionedMigration migrating storage version from {:?} to {:?}.",
				Pallet::name(),
				FROM,
				TO
			);

			// Execute the migration
			let weight = Inner::on_runtime_upgrade();

			// Update the on-chain version
			StorageVersion::new(TO).put::<Pallet>();

			weight.saturating_add(DbWeight::get().reads_writes(1, 1))
		} else {
			log::warn!(
				"🚚 Pallet {:?} VersionedMigration migration {}->{} can be removed; on-chain is already at {:?}.",
				Pallet::name(),
				FROM,
				TO,
				on_chain_version
			);
			DbWeight::get().reads(1)
		}
	}
```
