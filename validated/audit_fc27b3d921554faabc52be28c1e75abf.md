All core claims are directly verified in the repository code.

Audit Report

## Title
`claim_rewards_to` benchmark returns `None` for both bridge-hub-rococo relayer reward instances, causing the extrinsic's committed weight to encode `Weight::MAX` and permanently block alternative-beneficiary reward claims - (File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs)

## Summary
`pallet_bridge_relayers::benchmarking::Config::prepare_rewards_account` is documented to return `None` only as a fallback that forces `claim_rewards_to`'s benchmark result to `Weight::MAX`. Both of bridge-hub-rococo's implementations of this trait (`RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance`) unconditionally return `None`, and the committed, auto-generated weight files confirm this has already propagated into production: `claim_rewards_to()` in both weight files literally returns `Weight::from_parts(18_446_744_073_709_551_000, 0)`, i.e. `u64::MAX` ref-time. This value flows into `#[pallet::weight(T::WeightInfo::claim_rewards_to())]`, making the extrinsic permanently undispatchable for these two reward instances.

## Finding Description
The benchmarking contract in [1](#0-0)  states that `prepare_rewards_account` returning `None` "causes `claim_rewards_to` to be assigned `Weight::MAX`," and the benchmark itself enforces this via [2](#0-1) .

Bridge-hub-rococo's `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` implementations both explicitly `return None` after funding the rewards account, at [3](#0-2)  and [4](#0-3) .

This is not merely a theoretical benchmark artifact — the committed, auto-generated weight tables already reflect the poisoned value: [5](#0-4)  and [6](#0-5) , both encoding `Weight::from_parts(18_446_744_073_709_551_000, 0)` (`u64::MAX`) for `claim_rewards_to()`. This is the exact same defect that was identified and fixed for bridge-hub-westend's Snowbridge instance, as documented in [7](#0-6) , which explicitly states the bug was "causing `claim_rewards_to` to be assigned `Weight::MAX` and effectively disabling the extrinsic," and lists `bridge-hub-rococo-runtime` as a crate bumped by that fix — yet the rococo lib.rs source was not actually updated for the two non-Snowbridge instances.

## Impact Explanation
Any extrinsic whose declared weight is `u64::MAX` ref-time cannot be included in any block, since it will always exceed both the per-extrinsic and total block weight limits. This permanently blocks the `claim_rewards_to` call path for `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` on bridge-hub-rococo — relayers accumulating rewards under these instances have no way to redirect payout to an alternative beneficiary account. This matches the "permanent user-fund ... lock" / "public underpriced or overweight work that ... stalls bridge processing" impact category in the gate: legitimate, permissionless callers are denied a documented capability of the pallet with no workaround, purely due to a weight-computation defect rather than any protocol logic bug.

## Likelihood Explanation
This requires no privileged access, malicious actor, or off-chain assumption — the condition is deterministic and already baked into the committed weight files in this repository. Every ordinary relayer with reward balance under either affected instance who calls `claim_rewards_to` would be affected, with 100% reproducibility, as evidenced by the weight file contents matching `u64::MAX` ref-time exactly as the benchmark harness comment predicts.

## Recommendation
Update `bridge-hub-rococo`'s `prepare_rewards_account` implementations for both `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` to return `Some((reward_kind, beneficiary))` with a funded beneficiary account (mirroring the pattern already applied to bridge-hub-westend's Snowbridge instance), then regenerate and commit `pallet_bridge_relayers_legacy.rs` and `pallet_bridge_relayers_permissionless_lanes.rs` so `claim_rewards_to()` encodes a realistic, dispatchable weight. Add a CI check that flags any generated `WeightInfo` weight approaching `u64::MAX`/`Weight::MAX` to catch this pattern across all bridge-hub runtimes automatically.

## Proof of Concept
1. Inspect [8](#0-7)  — `claim_rewards_to()` returns `Weight::from_parts(18_446_744_073_709_551_000, 0)`, i.e., `u64::MAX` ref-time weight (same in the permissionless-lanes weight file).
2. This weight is used by `#[pallet::weight(T::WeightInfo::claim_rewards_to())]` on the live `claim_rewards_to` extrinsic for both `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` instances registered in bridge-hub-rococo's runtime.
3. Any signed transaction constructing this call is rejected by the transaction pool/block builder because the declared weight exceeds `BlockWeights::max_block` / per-extrinsic limits, regardless of relayer balance or reward state — this can be confirmed via a runtime integration test that registers a reward via `register_relayer_reward` and attempts to include a `claim_rewards_to` extrinsic in a `try_state`/`execute_block` scenario, which will fail the weight-limit check before dispatch logic executes.

### Citations

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

**File:** bridges/modules/relayers/src/benchmarking.rs (L95-105)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs (L1383-1403)
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

				fn deposit_account(account: AccountId, balance: Balance) {
					use frame_support::traits::fungible::Mutate;
					Balances::mint_into(&account, balance.saturating_add(ExistentialDeposit::get())).unwrap();
				}
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/weights/pallet_bridge_relayers_legacy.rs (L67-76)
```rust
	/// Storage: `Benchmark::Override` (r:0 w:0)
	/// Proof: `Benchmark::Override` (`max_values`: None, `max_size`: None, mode: `Measured`)
	fn claim_rewards_to() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `0`
		//  Estimated: `0`
		// Minimum execution time: 18_446_744_073_709_551_000 picoseconds.
		Weight::from_parts(18_446_744_073_709_551_000, 0)
			.saturating_add(Weight::from_parts(0, 0))
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/weights/pallet_bridge_relayers_permissionless_lanes.rs (L67-76)
```rust
	/// Storage: `Benchmark::Override` (r:0 w:0)
	/// Proof: `Benchmark::Override` (`max_values`: None, `max_size`: None, mode: `Measured`)
	fn claim_rewards_to() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `0`
		//  Estimated: `0`
		// Minimum execution time: 18_446_744_073_709_551_000 picoseconds.
		Weight::from_parts(18_446_744_073_709_551_000, 0)
			.saturating_add(Weight::from_parts(0, 0))
	}
```

**File:** prdoc/stable2606/pr_10952.prdoc (L1-7)
```text
title: Fix `claim_rewards_to` benchmark to enable Snowbridge reward claims
doc:
- audience: Runtime Dev
  description: The `prepare_rewards_account` benchmark helper was returning `None`,
    causing `claim_rewards_to` to be assigned `Weight::MAX` and effectively disabling
    the extrinsic. This fix returns a valid beneficiary account, enabling Snowbridge
    relayers to claim rewards to AssetHub as intended.
```
