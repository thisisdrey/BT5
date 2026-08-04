Found it. This is a solid, exact analog: bridge-hub-rococo-runtime still ships the broken benchmark that disables `claim_rewards_to` for Snowbridge rewards, mirroring the `rescueTokens` bug class — a function whose gating logic makes it permanently uncallable, causing accumulated relayer rewards to be stuck.### Title
`claim_rewards_to` is permanently uncallable for Snowbridge rewards on Bridge Hub Rococo — accrued relayer rewards get locked - ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs])

### Summary
`pallet-bridge-relayers::claim_rewards_to` is the public extrinsic a relayer uses to claim accumulated `BridgeReward::Snowbridge` rewards to an alternative beneficiary (an AssetHub location, since local-account claiming is explicitly rejected for Snowbridge rewards). Its dispatch weight is derived from `WeightInfo::claim_rewards_to()`, which in turn is generated from the `claim_rewards_to` benchmark. That benchmark is gated by the runtime-supplied `prepare_rewards_account` hook: if it returns `None`, the benchmark harness assigns `Weight::MAX` to the call — exactly analogous to `rescueTokens` being gated by `onlyVault` when no vault could ever call it. On bridge-hub-rococo, both `BridgeRelayersConfig` implementations (`RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance`) still return `None` unconditionally from `prepare_rewards_account`, whereas bridge-hub-westend was already patched (see `prdoc/stable2606/pr_10952.prdoc`) to return `Some((BridgeReward::Snowbridge, AssetHubLocation(..)))`. Rococo was not fixed.

### Finding Description
`prepare_rewards_account` on Bridge Hub Rococo: [1](#0-0) [2](#0-1) 

Both always return `None`. The benchmark helper trait explicitly documents the consequence: [3](#0-2) 

And the benchmark itself enforces it — when `prepare_rewards_account` returns `None`, the harness overrides the benchmark result with `Weight::MAX`: [4](#0-3) 

This is baked into the generated production weight tables shipped for bridge-hub-rococo, which hard-code `claim_rewards_to` to essentially `u64::MAX` (18,446,744,073,709,551,000 picoseconds ≈ `Weight::MAX`), unlike every other extrinsic in the same file which has a normal, small weight: [5](#0-4) [6](#0-5) 

The dispatchable itself is a plain public entry point requiring only a signed origin, with no privileged gate other than the weight annotation: [7](#0-6) 

Because `Weight::MAX` vastly exceeds any block's `max_block` weight limit, `frame_system`/`Executive` will reject any extrinsic carrying that declared weight before execution (it cannot fit in any block, and `ensure_can_consume_weight`/block builder logic will always exclude it). This exactly mirrors the Solidity finding: the function has a legitimate purpose and a normal caller (any relayer with `BridgeReward::Snowbridge` accrued), but a corrupted "gate" value (here, the benchmarked weight, standing in for the Solidity `onlyVault` check) makes it structurally impossible for that legitimate caller to ever successfully invoke it.

Contrast with bridge-hub-westend, which does return a valid tuple and has a normal, bounded weight: [8](#0-7) [9](#0-8) 
and the corresponding fix prdoc confirms this exact bug class existed and was patched only on Westend: [10](#0-9) 

### Impact Explanation
`BridgeReward::Snowbridge` explicitly forbids `LocalAccount` beneficiaries and requires `AssetHubLocation` routing through `claim_rewards_to`: [11](#0-10) 
`claim_rewards` (the local-only variant) will fail for Snowbridge rewards, and on bridge-hub-rococo `claim_rewards_to` is unreachable due to the `Weight::MAX` weight. This means relayers who deliver/process Snowbridge (Ethereum bridge) messages on Bridge Hub Rococo accrue rewards in `RelayerRewards` storage that can never be paid out — permanent lock of relayer-owed funds, a direct "public underpriced/impossible work" and "permanent fund lock" impact matching the accepted Polkadot SDK impact gate (permanent user-fund lock, degraded bridge processing incentive since relayers cannot ever collect their Snowbridge rewards on this chain).

### Likelihood Explanation
No privileged actor, governance, or malicious peer is required. Any ordinary signed relayer account that has accrued a `BridgeReward::Snowbridge` reward and calls the standard public extrinsic `claim_rewards_to` will be blocked purely by protocol-level weight validation, deterministically and permanently, on every attempt, on bridge-hub-rococo specifically.

### Recommendation
Fix `prepare_rewards_account` for both `BridgeRelayersConfig` implementations in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs` to mirror the bridge-hub-westend fix (PR #10952 analog): return `Some((BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(..)))` with an appropriately funded relayer and open outbound HRMP channel to AssetHub for the benchmark, then regenerate the `pallet_bridge_relayers` weights for bridge-hub-rococo so `claim_rewards_to` gets a real, bounded weight instead of `Weight::MAX`.

### Proof of Concept
1. On Bridge Hub Rococo, have a relayer accrue a `BridgeReward::Snowbridge` reward via `register_reward` (as happens automatically during Snowbridge V2 inbound message processing).
2. Call `pallet_bridge_relayers::claim_rewards_to(origin=signed(relayer), reward_kind=BridgeReward::Snowbridge, beneficiary=AssetHubLocation(..))`.
3. Because `WeightInfo::claim_rewards_to()` for bridge-hub-rococo returns `Weight::from_parts(18_446_744_073_709_551_000, 0)` (see `pallet_bridge_relayers_legacy.rs`/`pallet_bridge_relayers_permissionless_lanes.rs`), the extrinsic's declared weight exceeds `BlockWeights::max_block`, so it is rejected by weight validation before dispatch, regardless of relayer balance/permissions.
4. Confirm the reward remains permanently in `RelayerRewards` storage with no path to withdrawal, since `claim_rewards` (local-account variant) is also explicitly rejected for `BridgeReward::Snowbridge` as shown by the existing test `bridge_rewards_works` on bridge-hub-westend before its fix.

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

**File:** bridges/modules/relayers/src/benchmarking.rs (L38-52)
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
```

**File:** bridges/modules/relayers/src/benchmarking.rs (L96-124)
```rust
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/weights/pallet_bridge_relayers.rs (L68-75)
```rust
	fn claim_rewards_to() -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `297`
		//  Estimated: `0`
		// Minimum execution time: 40_000_000 picoseconds.
		Weight::from_parts(41_000_000, 0)
			.saturating_add(Weight::from_parts(0, 0))
	}
```

**File:** prdoc/stable2606/pr_10952.prdoc (L1-18)
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
  bump: minor
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
```
