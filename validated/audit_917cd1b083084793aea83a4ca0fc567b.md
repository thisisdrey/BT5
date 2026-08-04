### Title
Hardcoded, unverified cross-chain call-routing indices in `AhClientCalls`/`RelayChainRuntimePallets` can silently desynchronize from the real dispatchable, causing wrong-call execution with `Superuser`/Root origin - (File: `substrate/frame/staking-async/runtimes/parachain/src/staking.rs`, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs`, `substrate/frame/staking-async/runtimes/rc/src/lib.rs`)

### Summary
The external report's core defect is a hand-encoded function signature (`abi.encodeWithSignature`) that must byte-for-byte match the real function it targets; when it drifts from the actual signature, the computed selector routes to the wrong (or no) function. The local analog is the manually duplicated SCALE call-routing enums `RelayChainRuntimePallets` / `AhClientCalls` (and their `RcClientCalls`/`XcmCall` counterparts used elsewhere in the codebase, e.g. `bridges/relays/client-substrate/src/calls.rs`), which re-implement the pallet index and call index of `pallet-staking-async-ah-client` by hand and are used to build the `Transact` payload of XCM messages sent with `OriginKind::Superuser`. There is no compile-time or runtime assertion tying these hand-written indices to the actual `#[pallet::call_index]`/`#[runtime::pallet_index]` values of the real runtime, so any future reordering/insertion of pallets or calls (or copy/paste to a differently-indexed runtime) desynchronizes the encoding and causes the decoded call on the receiving chain to route to an unintended dispatchable.

### Finding Description
`RelayChainRuntimePallets`/`AhClientCalls` are plain `#[derive(Encode, Decode)]` enums that mirror, by hand, the pallet index of `pallet_staking_async_ah_client` in the production Westend runtime and the `call_index` of its dispatchables: [1](#0-0) 

The comment explicitly documents that this is a manually maintained mirror ("Call indices taken from westend-next runtime", "Audit: index of `AssetHubStakingClient` in westend"): [2](#0-1) 

This encoded byte string is then wrapped directly into an XCM `Transact` instruction and dispatched with elevated (`Superuser`/Root-mapped) origin on the counterpart chain: [3](#0-2) [4](#0-3) 

The receiving side (`pallet-staking-async-ah-client`) trusts the origin check inside each dispatchable (`AssetHubOrigin::ensure_origin_or_root`), but performs no independent verification that the SCALE bytes it receives actually correspond to the sender's intended call — it simply decodes whatever call index arrives at the outer `Call` enum position and dispatches it: [5](#0-4) [6](#0-5) 

This is structurally the same defect class as the reported Solidity bug: a hand-maintained encoding artifact (there, a function selector computed from a hardcoded signature string; here, a hardcoded `#[codec(index = N)]` pallet/call routing enum) that has to stay perfectly in sync with the real target's ABI/SCALE layout, with **no automated guard** (no `static_assert`, no `try-runtime` check, no shared derive from the real `Call` metadata) enforcing that sync across three independently maintained copies of the enum (`substrate/frame/staking-async/runtimes/rc/src/lib.rs`, `substrate/frame/staking-async/runtimes/parachain/src/staking.rs`, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs`). Similar hand-copied "shadow" `Call` enums exist project-wide for the same cross-chain-Transact pattern (e.g. `bridges/relays/client-substrate/src/calls.rs`, `cumulus/parachains/runtimes/assets/asset-hub-rococo/bridge-primitives/src/lib.rs`), all relying purely on developer diligence rather than a compiler/runtime-enforced invariant.

### Impact Explanation
If the pallet index (`67`) or a call's index inside `AhClientCalls`/`RelayChainRuntimePallets` diverges from the real `construct_runtime!`/`#[pallet::call_index]` layout of the target runtime — e.g. after a pallet is inserted/removed, a runtime is forked/rebuilt, or an entry is edited in one of the three duplicated files but not the others — the decoded `Transact` call executes an unintended dispatchable in the target pallet index/position, under `OriginKind::Superuser` (i.e., effectively Root on the receiving chain). Depending on what dispatchable now occupies that byte position, this can range from a silently dropped/failed call (denial of a legitimate validator-set/session-key update, potentially stalling relay-chain validator rotation) up to executing an entirely different privileged call with attacker- or sender-controlled arguments, which is exactly the "unauthorized execution / origin escalation" and "runtime bugs that compromise intended behavior" impact classes called out in the program scope.

### Likelihood Explanation
This is not exploitable by an external unprivileged attacker directly (the misencoding is triggered by legitimate runtime maintenance, not by malicious input), so likelihood of triggering purely via user input is low. However, unlike a typical one-off typo, this is a structural/process risk: the same manually-hardcoded indices are duplicated across at least three files (`rc/lib.rs`, `parachain/staking.rs`, `asset-hub-westend/src/staking.rs`) with no shared source of truth or automated cross-check, so every future runtime change to `pallet-staking-async-ah-client`'s call layout or its position in `construct_runtime!` is one missed manual edit away from silently corrupting cross-chain governance/session-key routing — the exact "incorrect signature accepted, mismatch discovered too late" scenario described in the external report.

### Recommendation
Replace the hand-written `#[codec(index = N)]` mirror enums with either (a) a shared crate/type generated from (or statically derived against) the real `pallet::Call` enum so the compiler enforces index equality, or (b) an automated `#[test]`/`try-runtime` check in CI that decodes a constructed `AhClientCalls`/`RelayChainRuntimePallets` value against the real runtime's `RuntimeCall` and asserts successful round-trip decode into the expected dispatchable variant, for every runtime that uses this pattern (`rc`, `parachain`, `asset-hub-westend`). Ideally add a defensive check on the receiving side that validates a message-specific discriminant (already present as e.g. `AssetHubOrigin`) in addition to raw call-index decoding.

### Proof of Concept
No dynamic PoC is provided: the vulnerability is a maintenance/verification gap (absence of an index-consistency invariant) rather than a directly triggerable state at the current commit. To demonstrate the risk conceptually: modify (or in a future PR, reorder) `pallet_staking_async_ah_client`'s pallet position from index `67` to any other value, or insert a new call before `validator_set` without an explicit `#[pallet::call_index]` on any pallet that is manually mirrored, without updating the three hand-copied enums in `substrate/frame/staking-async/runtimes/rc/src/lib.rs`, `substrate/frame/staking-async/runtimes/parachain/src/staking.rs`, and `cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs`; the existing test suite (e.g. `substrate/frame/staking-async/integration-tests/src/rc/test.rs`) calls the pallet's Rust functions directly rather than round-tripping through the actual SCALE-encoded XCM `Transact` payload, so no existing test would catch the desynchronization before it reached production.

**Note on verification limits:** I was unable to fully confirm within the available tool calls whether the currently checked-in index `67` is presently in sync with the production Westend runtime's `construct_runtime!` (the grep for `pallet_index(67)`/`StakingAhClient` in `polkadot/runtime/westend/src/lib.rs` returned matches but the final iteration cut off before I could inspect the exact line values). The finding above is therefore reported as a structural/process vulnerability (missing enforced invariant) rather than a confirmed present-day active mismatch; a Devin session with full repository access should verify the current index values across all three files to determine whether an active mismatch already exists.

### Citations

**File:** substrate/frame/staking-async/runtimes/parachain/src/staking.rs (L526-541)
```rust
#[derive(Encode, Decode)]
// Call indices taken from westend-next runtime.
pub enum RelayChainRuntimePallets {
	#[codec(index = 67)]
	AhClient(AhClientCalls),
}

#[derive(Encode, Decode)]
pub enum AhClientCalls {
	#[codec(index = 0)]
	ValidatorSet(rc_client::ValidatorSetReport<AccountId>),
	#[codec(index = 3)]
	SetKeysFromAh { stash: AccountId, keys: Vec<u8> },
	#[codec(index = 4)]
	PurgeKeysFromAh { stash: AccountId },
}
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/staking.rs (L543-550)
```rust
pub struct ValidatorSetToXcm;
impl Convert<rc_client::ValidatorSetReport<AccountId>, Xcm<()>> for ValidatorSetToXcm {
	fn convert(report: rc_client::ValidatorSetReport<AccountId>) -> Xcm<()> {
		rc_client::build_transact_xcm(
			RelayChainRuntimePallets::AhClient(AhClientCalls::ValidatorSet(report)).encode(),
		)
	}
}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs (L383-403)
```rust
#[derive(Encode, Decode)]
// Call indices taken from westend-next runtime.
pub enum RelayChainRuntimePallets {
	// Audit: index of `AssetHubStakingClient` in westend.
	#[codec(index = 67)]
	AhClient(AhClientCalls),
}

#[derive(Encode, Decode)]
pub enum AhClientCalls {
	// index of `fn validator_set` in `staking-async-ah-client`.
	#[codec(index = 0)]
	ValidatorSet(rc_client::ValidatorSetReport<AccountId>),
	// index of `fn set_keys_from_ah` in `staking-async-ah-client`.
	// Note: proof is validated on AH side, so only keys are sent to RC.
	#[codec(index = 3)]
	SetKeys { stash: AccountId, keys: Vec<u8> },
	// index of `fn purge_keys_from_ah` in `staking-async-ah-client`.
	#[codec(index = 4)]
	PurgeKeys { stash: AccountId },
}
```

**File:** substrate/frame/staking-async/runtimes/rc/src/lib.rs (L684-701)
```rust
pub struct SessionReportToXcm;
impl Convert<rc_client::SessionReport<AccountId>, Xcm<()>> for SessionReportToXcm {
	fn convert(a: rc_client::SessionReport<AccountId>) -> Xcm<()> {
		Xcm(vec![
			Instruction::UnpaidExecution {
				weight_limit: WeightLimit::Unlimited,
				check_origin: None,
			},
			Instruction::Transact {
				origin_kind: OriginKind::Superuser,
				fallback_max_weight: None,
				call: AssetHubRuntimePallets::RcClient(RcClientCalls::RelaySessionReport(a))
					.encode()
					.into(),
			},
		])
	}
}
```

**File:** substrate/frame/staking-async/ah-client/src/lib.rs (L535-551)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		#[pallet::call_index(0)]
		#[pallet::weight(
			// Reads:
			// - OperatingMode
			// - IncompleteValidatorSetReport
			// Writes:
			// - IncompleteValidatorSetReport or ValidatorSet
			// ignoring `T::SessionInterface::prune_up_to`
			T::DbWeight::get().reads_writes(2, 1)
		)]
		pub fn validator_set(
			origin: OriginFor<T>,
			report: rc_client::ValidatorSetReport<T::AccountId>,
		) -> DispatchResult {
			// Ensure the origin is one of Root or whatever is representing AssetHub.
```

**File:** substrate/frame/staking-async/ah-client/src/lib.rs (L641-656)
```rust
		/// Set session keys for a validator, forwarded from AssetHub.
		///
		/// This is called when a validator sets their session keys on AssetHub, which forwards
		/// the request to the RelayChain via XCM.
		///
		/// AssetHub validates both keys and ownership proof before sending.
		/// RC trusts AH's validation and does not re-validate.
		#[pallet::call_index(3)]
		#[pallet::weight(T::SessionInterface::set_keys_weight())]
		pub fn set_keys_from_ah(
			origin: OriginFor<T>,
			stash: T::AccountId,
			keys: Vec<u8>,
		) -> DispatchResult {
			T::AssetHubOrigin::ensure_origin_or_root(origin)?;
			log::info!(target: LOG_TARGET, "Received set_keys request from AssetHub for {stash:?}");
```
