### Title
Hardcoded `RelaySessionDuration` placeholder value corrupts `MaxEraDuration` and `PlanningEraOffset` derivation in `pallet_staking_async` - ([File: substrate/frame/staking-async/runtimes/parachain/src/staking.rs])

### Summary
The external VADER report describes a constant (`secondsPerEra`) that was set to a testing value (`1` second) instead of the intended production value (`86400` seconds), causing every downstream rate calculation that depends on that constant to be wrong by orders of magnitude. The closest local analog in this repository is the `RelaySessionDuration` constant in the `pallet_staking_async` runtime configuration, which is explicitly documented as needing to be "hardcoded per-runtime," and whose value diverges wildly between runtimes that should represent the same real-world quantity (relay-chain session length in AssetHub blocks).

### Finding Description
`RelaySessionDuration` is a `parameter_types!` constant meant to represent "duration of a relay session in our blocks" [1](#0-0) . In the `asset-hub-westend` runtime it is correctly set to `1 * HOURS` [2](#0-1) , but in `substrate/frame/staking-async/runtimes/parachain/src/staking.rs` — a template runtime intended to be reused for real deployments — the same constant is hardcoded to `10` (blocks), an obvious placeholder/testing value rather than a real session length [3](#0-2) .

This constant feeds two safety-critical derived values:

1. `MaxEraDuration`, the defensive cap on era duration used to prevent runaway inflation when `DisableMinting = false`:
`MaxEraDuration = RelaySessionDuration * RELAY_CHAIN_SLOT_DURATION_MILLIS * SessionsPerEra` [4](#0-3) . This is exactly analogous to `secondsPerEra` in VADER: a hardcoded time-unit constant whose value is silently far smaller than the real quantity it is meant to represent, deflating the computed cap by ~2-3 orders of magnitude versus the correctly-configured westend value (`1 * HOURS` vs `10` blocks).

2. `PlanningEraOffset`, computed via `PlanningEraOffsetOf<Self, RelaySessionDuration, ConstU32<10>>` [5](#0-4) , whose implementation divides the election duration by `RS::get()` (i.e. `RelaySessionDuration`):
```rust
let sessions_needed = (election_duration + S::get()) / RS::get();
``` [6](#0-5) 

If `RelaySessionDuration` is understated (as it is with the placeholder value `10`), `sessions_needed` is inflated, and the resulting `PlanningEraOffset` gets clamped to `SessionsPerEra` in `is_plan_era_deadline`:
```rust
let planning_era_offset = T::PlanningEraOffset::get().min(T::SessionsPerEra::get());
let target_plan_era_session = T::SessionsPerEra::get().saturating_sub(planning_era_offset);
``` [7](#0-6) 

With the offset clamped to `SessionsPerEra`, `target_plan_era_session` collapses to `0`, meaning the pallet is perpetually in "plan new era" mode from the very first session of every era — a direct consequence of the miscalibrated time-unit constant, mirroring how VADER's mis-scaled `secondsPerEra` silently corrupted every downstream rate computation.

### Impact Explanation
This corrupts two safety-critical, chain-liveness-relevant computations derived from a single mis-scaled constant:
- `MaxEraDuration` no longer reflects a realistic "1 day"-scale safety ceiling on era duration and legacy inflation payout (relevant if `DisableMinting` is ever flipped to `false` on a runtime derived from this template).
- `PlanningEraOffset` collapses to always-plan-immediately behavior, which can desynchronize AssetHub-side election timing from the real relay-chain session cadence, risking missed election windows/validator-set export deadlines and stalled era rotation — a chain-liveness impact.

Since this template is explicitly commented "Needs to be hardcoded per-runtime," any downstream production runtime copying this file without updating the constant inherits a testing-scale value in a production deployment, exactly matching the VADER root cause: a testing constant silently reaching production because there is no runtime-level validation enforcing internal consistency between `RelaySessionDuration` and the actual relay-chain session length.

### Likelihood Explanation
Moderate-to-low confidence. This is a configuration-integrity issue rather than an attacker-triggerable exploit path — there is no unprivileged transaction that lets an attacker corrupt this value; the risk materializes only if a production runtime is derived from this template without correcting the placeholder, similar to how the VADER bug survived because a testing parameter was "acknowledged" as intentional but never fixed before mainnet deployment. I could not verify from the index whether `substrate/frame/staking-async/runtimes/parachain/` is actually instantiated as a live production runtime elsewhere in the repo (e.g. Kusama AssetHub) or remains solely a reference/example template — this would need to be confirmed with a full checkout since the index may not include all downstream runtime crates that consume this template.

### Recommendation
- Add a compile-time or `integrity_test()`-style runtime assertion (similar to the one already present in `pallet_dap::Config::integrity_test` for `MaxElapsedPerDrip > IssuanceCadence`) that validates `RelaySessionDuration` against `EXPECTED_BLOCK_TIME`/`RELAY_CHAIN_SLOT_DURATION_MILLIS` to catch obviously wrong (too small) values before deployment.
- Replace the placeholder `10` in `substrate/frame/staking-async/runtimes/parachain/src/staking.rs` with a realistic relay-session-length value (or clearly gate it behind `prod_or_fast!` as done for `SessionsPerEra`) so "fast" testing values cannot silently ship as the production default.
- Add a unit test asserting `PlanningEraOffsetOf::get()` produces a sane, non-degenerate offset (i.e., strictly less than `SessionsPerEra`) under the runtime's configured constants.

### Proof of Concept
1. Instantiate `pallet_staking_async::Config` using the `substrate/frame/staking-async/runtimes/parachain` template as-is, with `RelaySessionDuration = 10` and `SessionsPerEra = prod_or_fast!(6, 1)`.
2. Call `PlanningEraOffsetOf::<Runtime, RelaySessionDuration, ConstU32<10>>::get()` — the numerator `(election_duration + 10)` divided by the tiny denominator `10` yields a `sessions_needed` value that, after `+1+1`, exceeds `SessionsPerEra`.
3. Observe in `session_rotation::EraElectionPlanner::is_plan_era_deadline` that `planning_era_offset.min(SessionsPerEra)` clamps to `SessionsPerEra`, making `target_plan_era_session == 0`, so the pallet begins planning the next era at the very first session of every era instead of the intended lead time before era end — demonstrating that the mis-scaled constant collapses the intended timing behavior, directly analogous to VADER's `secondsPerEra` collapsing the intended daily emission cadence to a per-second cadence.

### Citations

**File:** substrate/frame/staking-async/runtimes/parachain/src/staking.rs (L410-414)
```rust
parameter_types! {
	// Six sessions in an era (6 hours).
	pub const SessionsPerEra: SessionIndex = prod_or_fast!(6, 1);
	/// Duration of a relay session in our blocks. Needs to be hardcoded per-runtime.
	pub const RelaySessionDuration: BlockNumber = 10;
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/staking.rs (L426-426)
```rust
	pub const MaxEraDuration: u64 = RelaySessionDuration::get() as u64 * RELAY_CHAIN_SLOT_DURATION_MILLIS as u64 * SessionsPerEra::get() as u64;
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/staking.rs (L464-465)
```rust
	type PlanningEraOffset =
		pallet_staking_async::PlanningEraOffsetOf<Self, RelaySessionDuration, ConstU32<10>>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs (L264-265)
```rust
	/// Duration of a relay session in our blocks. Needs to be hardcoded per-runtime.
	pub const RelaySessionDuration: BlockNumber = 1 * HOURS;
```

**File:** substrate/frame/staking-async/src/lib.rs (L730-742)
```rust
impl<T: Config, RS: Get<BlockNumberFor<T>>, S: Get<BlockNumberFor<T>>> Get<SessionIndex>
	for PlanningEraOffsetOf<T, RS, S>
{
	fn get() -> SessionIndex {
		let election_duration = <T::ElectionProvider as ElectionProvider>::duration_with_export();
		let sessions_needed = (election_duration + S::get()) / RS::get();
		// add one, because we know the RC session pallet wants to buffer for one session, and
		// another one cause we will receive activation report one session after that.
		sessions_needed
			.saturating_add(One::one())
			.saturating_add(One::one())
			.unique_saturated_into()
	}
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L1027-1043)
```rust
	/// Returns whether we are at the session where we should plan the new era.
	fn is_plan_era_deadline(start_session: SessionIndex) -> bool {
		let planning_era_offset = T::PlanningEraOffset::get().min(T::SessionsPerEra::get());
		// session at which we should plan the new era.
		let target_plan_era_session = T::SessionsPerEra::get().saturating_sub(planning_era_offset);
		let era_start_session = Self::active_era_start_session_index();

		// progress of the active era in sessions.
		let session_progress = start_session.defensive_saturating_sub(era_start_session);

		log!(
			debug,
			"Session progress within era: {:?}, target_plan_era_session: {:?}",
			session_progress,
			target_plan_era_session
		);
		session_progress >= target_plan_era_session
```
