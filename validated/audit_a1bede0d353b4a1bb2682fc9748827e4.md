### Title
`claim_rewards_to` benchmark returns `None` for bridge-hub-rococo relayer reward instances, poisoning the extrinsic's weight and permanently blocking alternative-beneficiary reward claims - ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs])

### Summary
The external report's core defect is: a critical state-dependent value (`_rewardDistributors`) is left uninitialized, so a security-critical function silently fails to do its job even though nothing else visibly errors. The exact analog exists in `paritytech/polkadot-sdk`'s bridge-hub-rococo runtime: the `prepare_rewards_account` benchmark helper for both bridge-relayers reward instances explicitly returns `None`, which by design causes the `claim_rewards_to` extrinsic to be assigned `Weight::MAX`. This is the same class of bug already documented and *fixed only for bridge-hub-westend* in `prdoc/stable2606/pr_10952.prdoc`, but the analogous rococo config was left unpatched.

### Finding Description
`pallet_bridge_relayers`'s benchmarking harness defines the contract explicitly: [1](#0-0) 

and the `claim_rewards_to` benchmark enforces that contract: [2](#0-1) 

If `prepare_rewards_account` returns `None`, the benchmark is overridden with `BenchmarkResult::from_weight(Weight::MAX)` — i.e., the measured weight for the `claim_rewards_to` call becomes `Weight::MAX`. This value then flows into the generated `WeightInfo::claim_rewards_to()` implementation used by the live pallet (`#[pallet::weight(T::WeightInfo::claim_rewards_to())]`), and a call whose declared weight is `Weight::MAX` can never fit within a block's maximum extrinsic/block weight, so the extrinsic becomes permanently undispatchable at runtime — functionally identical to the report's "the intended external call path never executes."

On `bridge-hub-westend`, this was already identified and fixed: the runtime's `prepare_rewards_account` for the `Snowbridge` reward kind now returns `Some(...)` with a real beneficiary: [3](#0-2) 

confirmed by the fix's own changelog: [4](#0-3) 

However, `bridge-hub-rococo`'s equivalent trait implementations for both `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` still explicitly return `None`, unlike westend: [5](#0-4) [6](#0-5) 

These runtime-benchmarks-only trait impls directly drive the committed weight tables used at runtime for those two instances (`pallet_bridge_relayers_legacy.rs` and `pallet_bridge_relayers_permissionless_lanes.rs`), which the mainline pallet consults via `T::WeightInfo::claim_rewards_to()`.

### Impact Explanation
Relayer rewards accumulated under `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` on bridge-hub-rococo (registered via `register_reward`, as demonstrated in the rococo/westend parity test) can only be claimed to an alternative beneficiary through `claim_rewards_to`. If that extrinsic's declared weight is `Weight::MAX`, no signed transaction calling it can ever be included in a block (it will always exceed both per-extrinsic and total block weight limits). This satisfies the "permanent user-fund ... lock" and "public underpriced/overweight work that ... stalls bridge processing" impact categories: legitimate relayers who need to route rewards to a different account (e.g., a hot/cold wallet split, or cross-chain beneficiary) are permanently unable to do so for these two reward kinds, with no workaround inside the protocol itself, mirroring the `VeQoda` bug where an unprivileged, permissionless code path silently fails to perform its intended state-changing action due to an uninitialized/`None` dependency.

### Likelihood Explanation
This is not a hypothetical: the identical failure mode was already discovered and remediated for the Snowbridge/westend instance in this exact codebase (`pr_10952.prdoc`). The rococo-side implementations for the two other reward instances still contain the pre-fix `None` return, meaning the underlying condition-for-bug is proven to exist in this repository's source and was not consistently fixed across all bridge-hub runtimes that share the same `pallet_bridge_relayers::benchmarking::Config` trait. No malicious actor, governance action, or privileged operator is needed — an ordinary relayer simply calling `claim_rewards_to` triggers the fault; the only requirement is that the corresponding weights be regenerated/committed from this benchmark configuration, which is standard, automated CI/benchmarking practice for these runtimes.

### Recommendation
Update `bridge-hub-rococo`'s `prepare_rewards_account` implementations for `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` to return `Some((reward_kind, beneficiary))` with a properly funded beneficiary, mirroring the westend fix in `pr_10952`, then regenerate and commit the corresponding weight files (`pallet_bridge_relayers_legacy.rs`, `pallet_bridge_relayers_permissionless_lanes.rs`) so `claim_rewards_to` receives a realistic, dispatchable weight. As defense-in-depth, add a CI/try-runtime check asserting that no generated `WeightInfo` weight for any public extrinsic approaches `Weight::MAX`, to catch this benchmark-helper-returns-`None` pattern automatically across all bridge-hub runtimes.

### Proof of Concept
1. Build `bridge-hub-rococo-runtime` with `runtime-benchmarks` enabled and run the `pallet_bridge_relayers` benchmarks for `RelayersForLegacyLaneIdsMessagesInstance`/`RelayersForPermissionlessLanesInstance`.
2. `prepare_rewards_account` returns `None` (per `bridge-hub-rococo/src/lib.rs:1383-1403` and `:1414-1428`), forcing `claim_rewards_to`'s benchmark result to `BenchmarkResult::from_weight(Weight::MAX)` (`bridges/modules/relayers/src/benchmarking.rs:100-104`).
3. The generated `WeightInfo::claim_rewards_to()` for these two instances therefore encodes `Weight::MAX` (or a value effectively unusable in a block).
4. At runtime, a relayer who has an accumulated reward under `RelayerRewards` for either instance (e.g., via `register_reward`, as done in `bridge-hub-westend/tests/tests.rs:729-731`) submits `claim_rewards_to(reward_kind, alternative_beneficiary)`; the transaction pool/block builder rejects it for exceeding weight limits, so the reward can never be redirected to the alternative beneficiary — permanently, since there is no other entry point exposing this capability for these reward kinds.

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
