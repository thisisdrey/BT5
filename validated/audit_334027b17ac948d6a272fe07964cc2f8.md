This is a strong local analog: `bridge-hub-rococo`'s `prepare_rewards_account` implementations both return `None` (rather than `Some(...)`), which per the trait's own documented contract in `bridges/modules/relayers/src/benchmarking.rs` causes `claim_rewards_to`'s weight to be pinned at `Weight::MAX`, effectively disabling the extrinsic for that runtime — exactly the "missing setup call → fee/reward permanently unclaimable" pattern from the external report (`launchV4Pool()` omitting `lpLocker.setTokenParams()`).

### Title
`claim_rewards_to` is left permanently disabled on Bridge Hub Rococo because `prepare_rewards_account` never returns a beneficiary - ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs])

### Summary
Bridge Hub Westend was already patched (see `prdoc/stable2606/pr_10952.prdoc`) because its `prepare_rewards_account` benchmark helper used to return `None`, which caused the `claim_rewards_to` extrinsic's weight to be computed as `Weight::MAX`, making the call practically un-callable (any block-weight-limited transaction with `Weight::MAX` will be rejected as exceeding the block/extrinsic weight limit). The same root cause still exists in `bridge-hub-rococo`: both `BridgeRelayersConfig` implementations for `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` explicitly `return None` from `prepare_rewards_account`. [1](#0-0) [2](#0-1) 

### Finding Description
The `claim_rewards_to` extrinsic lets a relayer claim accumulated bridge rewards and route them to an alternative beneficiary via `Self::do_claim_rewards`. [3](#0-2) 

Its benchmark weight is derived from the `claim_rewards_to` benchmark in `bridges/modules/relayers/src/benchmarking.rs`. That benchmark explicitly documents: "Returning `None` [from `prepare_rewards_account`] causes `claim_rewards_to` to be assigned `Weight::MAX`," and the code does exactly that via `BenchmarkResult::from_weight(Weight::MAX)` when `prepare_rewards_account` returns `None`. [4](#0-3) [5](#0-4) 

Both `BridgeRelayersConfig` implementations wired into the Bridge Hub Rococo runtime's `impl_benchmark_test_suite`/benchmark list unconditionally `return None`, mirroring the exact bug that was already found and fixed for Bridge Hub Westend in PR #10952 ("Fix `claim_rewards_to` benchmark to enable Snowbridge reward claims" — "The `prepare_rewards_account` benchmark helper was returning `None`, causing `claim_rewards_to` to be assigned `Weight::MAX` and effectively disabling the extrinsic"). [6](#0-5) 

The parallel with the external report is direct: `launchV4Pool()` skipped `lpLocker.setTokenParams()` and left fee-claim logic non-functional for one code path while the other path (`graduateToken()`) worked correctly. Here, `prepare_rewards_account` for Bridge Hub Rococo skips returning the `(reward_kind, beneficiary)` pair needed to correctly benchmark/weight `claim_rewards_to`, leaving that extrinsic non-functional on Rococo's Bridge Hub, while the parallel Westend implementation was already fixed to return `Some(...)`.

### Impact Explanation
If a generated weight of `Weight::MAX` is baked into the runtime's `WeightInfo` for `claim_rewards_to`, the extrinsic will always exceed the maximum block weight / extrinsic weight limits, so any relayer attempting to claim Snowbridge (or other) rewards routed through `claim_rewards_to` on Bridge Hub Rococo will have their transaction rejected before execution. This causes accumulated bridge-relayer rewards registered via `register_reward`/`RelayerRewards` storage to become permanently unclaimable through this path — a fund-lock condition for legitimate reward recipients, matching the "permanent user-fund ... lock" impact category in the gate criteria.

### Likelihood Explanation
This is not a hypothetical: the identical pattern was already discovered and remediated for Bridge Hub Westend (PR #10952), confirming the bug class is real and has previously affected production runtime weight generation in this codebase. The Rococo runtime's benchmarking config, read directly from the current repository state, still contains the unfixed `None`-returning implementations for both `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance`, making this a live, reproducible condition purely from re-running/regenerating weights for Bridge Hub Rococo — no privileged actor, governance, or external protocol assumption is required.

### Recommendation
Update `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs`'s two `BridgeRelayersConfig::prepare_rewards_account` implementations to fund an account and return `Some((reward_kind, beneficiary))`, mirroring the fix applied to Bridge Hub Westend in PR #10952, then regenerate weights for `pallet-bridge-relayers` on Bridge Hub Rococo (`pallet_bridge_relayers_legacy.rs` / `pallet_bridge_relayers_permissionless_lanes.rs`) so `claim_rewards_to` receives a real, non-`Weight::MAX` weight.

### Proof of Concept
1. Run runtime benchmarks for `pallet_bridge_relayers` against the Bridge Hub Rococo runtime (`cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo`), exercising the `BridgeRelayersConfig` impls at lines 1374–1434 of `lib.rs`.
2. Because `prepare_rewards_account` returns `None` in both instances, the `claim_rewards_to` benchmark in `bridges/modules/relayers/src/benchmarking.rs` (lines 95–104) hits the `else` branch and returns `BenchmarkResult::from_weight(Weight::MAX)`.
3. The generated `WeightInfo::claim_rewards_to()` for Bridge Hub Rococo is `Weight::MAX`.
4. Any relayer signed extrinsic calling `Pallet::claim_rewards_to(origin, reward_kind, beneficiary)` on Bridge Hub Rococo is rejected by the transaction-weight/block-weight checks before dispatch, so registered rewards (`RelayerRewards` storage, populated via `register_reward`) can never be claimed to an alternative beneficiary on that chain — analogous to LP fees being permanently unclaimable via `launchV4Pool()`.

### Citations

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs (L1383-1397)
```rust
				fn prepare_rewards_account(
					_relayer: &AccountId,
					reward_kind: Self::Reward,
					reward: Balance,
				) -> Option<(Self::Reward, AccountId)> {
					let rewards_account = bp_relayers::PayRewardFromAccount::<
						Balances,
						AccountId,
						bp_messages::LegacyLaneId,
						Balance,
					>::rewards_account(reward_kind);
					<Runtime as BridgeRelayersConfig<bridge_common_config::RelayersForLegacyLaneIdsMessagesInstance>>::deposit_account(rewards_account, reward);

					None
				}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs (L1414-1428)
```rust
				fn prepare_rewards_account(
					_relayer: &AccountId,
					reward_kind: Self::Reward,
					reward: Balance,
				) -> Option<(Self::Reward, AccountId)> {
					let rewards_account = bp_relayers::PayRewardFromAccount::<
						Balances,
						AccountId,
						bp_messages::HashedLaneId,
						Balance,
					>::rewards_account(reward_kind);
					<Runtime as BridgeRelayersConfig<bridge_common_config::RelayersForPermissionlessLanesInstance>>::deposit_account(rewards_account, reward);

					None
				}
```

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
		}
```

**File:** bridges/modules/relayers/src/benchmarking.rs (L38-55)
```rust
	/// Prepare environment for paying the given reward, and optionally return the
	/// `(reward_kind, beneficiary)` pair to use for the `claim_rewards_to`
	/// benchmark. Returning `Some` enables that benchmark and lets the runtime
	/// pick a different reward kind than `bench_reward()` for it (e.g., a
	/// Snowbridge reward routed via XCM to an `AssetHubLocation`, which is not
	/// valid for the basic `claim_rewards` extrinsic). Implementations should
	/// also fund `relayer` with whatever balance the payment path needs (e.g.,
	/// XCM delivery fees).
	///
	/// Returning `None` causes `claim_rewards_to` to be assigned `Weight::MAX`.
	fn prepare_rewards_account(
		relayer: &Self::AccountId,
		reward_kind: Self::Reward,
		reward: Self::RewardBalance,
	) -> Option<(Self::Reward, BeneficiaryOf<Self, I>)>;
	/// Give enough balance to given account.
	fn deposit_account(account: Self::AccountId, balance: Self::Balance);
}
```

**File:** bridges/modules/relayers/src/benchmarking.rs (L95-124)
```rust
	#[benchmark]
	fn claim_rewards_to() -> Result<(), BenchmarkError> {
		let relayer: T::AccountId = whitelisted_caller();
		let reward_balance = T::RewardBalance::from(REWARD_AMOUNT);

		let Some((reward_kind, alternative_beneficiary)) =
			T::prepare_rewards_account(&relayer, T::bench_reward(), reward_balance)
		else {
			return Err(BenchmarkError::Override(BenchmarkResult::from_weight(Weight::MAX)));
		};
		RelayerRewards::<T, I>::insert(&relayer, reward_kind, reward_balance);

		#[extrinsic_call]
		_(RawOrigin::Signed(relayer.clone()), reward_kind, alternative_beneficiary.clone());

		// we can't check anything here, because `PaymentProcedure` is responsible for
		// payment logic, so we assume that if call has succeeded, the procedure has
		// also completed successfully
		assert_last_event::<T, I>(
			Event::RewardPaid {
				relayer: relayer.clone(),
				reward_kind,
				reward_balance,
				beneficiary: alternative_beneficiary,
			}
			.into(),
		);

		Ok(())
	}
```

**File:** prdoc/stable2606/pr_10952.prdoc (L1-17)
```text
title: Fix `claim_rewards_to` benchmark to enable Snowbridge reward claims
doc:
- audience: Runtime Dev
  description: The `prepare_rewards_account` benchmark helper was returning `None`,
    causing `claim_rewards_to` to be assigned `Weight::MAX` and effectively disabling
    the extrinsic. This fix returns a valid beneficiary account, enabling Snowbridge
    relayers to claim rewards to AssetHub as intended.
crates:
- name: bridge-hub-rococo-runtime
  bump: minor
- name: bridge-hub-westend-runtime
  bump: minor
- name: bp-bridge-hub-westend
  bump: minor
- name: pallet-bridge-relayers
  bump: minor
- name: bp-runtime
```
