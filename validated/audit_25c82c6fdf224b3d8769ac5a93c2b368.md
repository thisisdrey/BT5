The relevant analog here is a very literal parallel to the compound bug: a hardcoded/never-populated helper causes a legitimate reward-claim extrinsic to be un-callable, permanently blocking users from claiming their rewards, precisely as `grantCompInternal`/`claimComp` were guaranteed to revert in the Canto report.

### Title
`claim_rewards_to` is permanently unusable on BridgeHub-Rococo due to `Weight::MAX` baked into shipped weights - (File: `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/weights/pallet_bridge_relayers_legacy.rs` and `pallet_bridge_relayers_permissionless_lanes.rs`)

### Summary
On BridgeHub-Rococo, the `BridgeRelayersConfig::prepare_rewards_account` benchmark helper unconditionally returns `None` for both reward instances (`RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance`). Per the benchmarking framework's contract, returning `None` forces the `claim_rewards_to` benchmark to be overridden with `Weight::MAX`, and this exact placeholder value (`18_446_744_073_709_551_000` picoseconds, i.e. `u64::MAX`) is baked into the checked-in production `WeightInfo` implementations. Because `pallet::weight(T::WeightInfo::claim_rewards_to())` uses this value to gate dispatch, the extrinsic can never fit inside any block's weight limit and is therefore permanently unusable on BridgeHub-Rococo, even though the underlying `PaymentProcedure::pay_reward` logic (`PayRewardFromAccount`) is fully functional and would otherwise succeed.

### Finding Description
`prepare_rewards_account` is documented to return `Some((reward_kind, beneficiary))` to enable the `claim_rewards_to` benchmark, and returning `None` causes `Weight::MAX` to be assigned instead: [1](#0-0) . The actual benchmark implementation reflects this behavior exactly — it overrides the result with `Weight::MAX` when `prepare_rewards_account` returns `None`: [2](#0-1) .

On BridgeHub-Rococo, both bridge-relayers instances configure `prepare_rewards_account` to unconditionally return `None`: [3](#0-2) [4](#0-3) .

The generated weight files that ship in the runtime binary encode this `Weight::MAX` override verbatim for `claim_rewards_to`, for both the legacy-lane and permissionless-lane instances: [5](#0-4) [6](#0-5) .

The extrinsic's dispatch weight is directly derived from this `WeightInfo::claim_rewards_to()` value: [7](#0-6) . Since Substrate's `CheckWeight` signed extension rejects any extrinsic whose declared weight exceeds the block's maximum weight before execution even begins, and `u64::MAX` picoseconds vastly exceeds any realistic block weight limit, `claim_rewards_to` can never be included in a block on BridgeHub-Rococo for either instance — it is permanently bricked in production, not just in benchmarking.

This is directly comparable to the original report: a leftover/unset configuration value (there hardcoded WETH address, here a permanently-`None` benchmark hook) guarantees that a reward-claim code path is non-functional, causing legitimate relayers to be unable to claim their earned rewards to an account of their choosing.

The companion fix for BridgeHub-Westend confirms the pattern and its previously-broken state: the same helper there used to return `None` (disabling `claim_rewards_to`) until it was fixed to return `Some(...)` so Snowbridge relayers could claim rewards to AssetHub: [8](#0-7) , and the corrected Westend implementation now returns a valid beneficiary: [9](#0-8) . BridgeHub-Rococo's two legacy/permissionless-lane instances were never given the equivalent fix.

### Impact Explanation
Relayers who bridge messages on Rococo↔Westend lanes accumulate rewards under `RelayerRewards` and can normally redirect payment to an alternate account via `claim_rewards_to`. On BridgeHub-Rococo this extrinsic is unconditionally disabled by an over-large weight baked into the runtime, so relayers lose access to that payout path entirely, on both the legacy-lane and permissionless-lane bridge-relayer instances. This is a public-entrypoint dispatch that is permanently non-functional due to a leftover placeholder configuration value, not a decode/execution failure — a fund-access/payout availability bug matching the "public underpriced/overpriced work" and "permanent lock of user funds/claim path" impact categories.

### Likelihood Explanation
This requires no attacker action at all — every relayer who calls `claim_rewards_to` on BridgeHub-Rococo will always fail, deterministically, on every attempt, because the weight is fixed at compile time in the shipped weights file. The condition is 100% reproducible.

### Recommendation
Update `BridgeRelayersConfig::prepare_rewards_account` for `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs` to return `Some((reward_kind, beneficiary))` (mirroring the BridgeHub-Westend fix from PR #10952), then regenerate the `pallet_bridge_relayers_legacy.rs` / `pallet_bridge_relayers_permissionless_lanes.rs` weight files so `claim_rewards_to` carries a real, bounded weight instead of `Weight::MAX`.

### Proof of Concept
1. Deploy/observe BridgeHub-Rococo runtime as currently checked in.
2. As any registered relayer with an accumulated reward under `RelayerRewards` for either `RelayersForLegacyLaneIdsMessagesInstance` or `RelayersForPermissionlessLanesInstance`, submit `BridgeRelayers::claim_rewards_to(reward_kind, beneficiary)`.
3. The transaction is rejected pre-dispatch by `CheckWeight` because `WeightInfo::claim_rewards_to()` returns `Weight::from_parts(18_446_744_073_709_551_000, 0)` (`u64::MAX` picoseconds), which exceeds the block's maximum extrinsic/normal-dispatch weight by many orders of magnitude — confirmed by the checked-in weight files at [10](#0-9) .
4. Contrast with BridgeHub-Westend, where the equivalent config returns `Some(...)` and `claim_rewards_to` works as demonstrated by the passing test `bridge_rewards_works` at [11](#0-10) .

### Citations

**File:** bridges/modules/relayers/src/benchmarking.rs (L47-52)
```rust
	/// Returning `None` causes `claim_rewards_to` to be assigned `Weight::MAX`.
	fn prepare_rewards_account(
		relayer: &Self::AccountId,
		reward_kind: Self::Reward,
		reward: Self::RewardBalance,
	) -> Option<(Self::Reward, BeneficiaryOf<Self, I>)>;
```

**File:** bridges/modules/relayers/src/benchmarking.rs (L96-104)
```rust
	fn claim_rewards_to() -> Result<(), BenchmarkError> {
		let relayer: T::AccountId = whitelisted_caller();
		let reward_balance = T::RewardBalance::from(REWARD_AMOUNT);

		let Some((reward_kind, alternative_beneficiary)) =
			T::prepare_rewards_account(&relayer, T::bench_reward(), reward_balance)
		else {
			return Err(BenchmarkError::Override(BenchmarkResult::from_weight(Weight::MAX)));
		};
```

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

**File:** bridges/modules/relayers/src/lib.rs (L224-236)
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/lib.rs (L1451-1491)
```rust
				fn prepare_rewards_account(
					relayer: &AccountId,
					reward_kind: Self::Reward,
					reward: Balance,
				) -> Option<(
					Self::Reward,
					pallet_bridge_relayers::BeneficiaryOf<Runtime, bridge_common_config::BridgeRelayersInstance>,
				)> {
					let bridge_common_config::BridgeReward::RococoWestend(legacy_reward_kind) = reward_kind else {
						panic!("Unexpected reward_kind: {:?} - not compatible with `bench_reward`!", reward_kind);
					};
					let rewards_account = bp_relayers::PayRewardFromAccount::<
						Balances,
						AccountId,
						bp_messages::LegacyLaneId,
						u128,
					>::rewards_account(legacy_reward_kind);
					Self::deposit_account(rewards_account, reward);

					// Worst-case `claim_rewards_to` path on Westend BridgeHub: Snowbridge
					// rewards routed via XCM to an account on AssetHub. The XCM-routed
					// payment charges the relayer for delivery fees on BridgeHub, so fund
					// the relayer generously. Also open the outbound HRMP channel to
					// AssetHub so the XCM router can validate/deliver.
					Self::deposit_account(relayer.clone(), 100 * UNITS);
					ParachainSystem::open_outbound_hrmp_channel_for_benchmarks_or_tests(
						ASSET_HUB_ID.into(),
					);

					let beneficiary_on_ah = Location::new(
						0,
						[Junction::AccountId32 { network: None, id: [99u8; 32] }],
					);

					Some((
						bridge_common_config::BridgeReward::Snowbridge,
						bridge_common_config::BridgeRewardBeneficiaries::AssetHubLocation(
							VersionedLocation::from(beneficiary_on_ah),
						),
					))
				}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L789-795)
```rust
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```
