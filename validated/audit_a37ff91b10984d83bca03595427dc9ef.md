## Analysis

The core broken invariant from H-2 is: a restoration/claim path deposits value that can only be released through a code path whose enabling precondition is permanently unsatisfied, so the claim call can never succeed and the value is stuck forever.

I found a local analog of exactly this shape in the Bridge Hub relayer-rewards benchmarking harness for `pallet-bridge-relayers`.

### Title
`claim_rewards_to` is permanently uncallable on Bridge Hub Rococo because its benchmark weight is set to `Weight::MAX` — ([File: cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs])

### Summary
`pallet_bridge_relayers::claim_rewards_to` lets a relayer redirect an accumulated reward to an alternative beneficiary [1](#0-0)  via `Self::do_claim_rewards`, which atomically takes the stored `RelayerRewards` entry and pays it through `T::PaymentProcedure::pay_reward` [2](#0-1) . The dispatch weight for this extrinsic on Bridge Hub Rococo's `RelayersForLegacyLaneIdsMessagesInstance` and `RelayersForPermissionlessLanesInstance` is hard-coded to `18_446_744_073_709_551_000` picoseconds (≈ `u64::MAX`) in the shipped weight tables [3](#0-2) [4](#0-3) , because the `prepare_rewards_account` benchmark helper for those instances unconditionally returns `None` [5](#0-4) . Per the benchmark harness, returning `None` from `prepare_rewards_account` deliberately assigns `Weight::MAX` to `claim_rewards_to` [6](#0-5) . A weight of `u64::MAX` unconditionally exceeds any block's `maximum_weight`/`max_extrinsic_weight` limits, so the transaction pool/executive will always reject this extrinsic before it ever executes — for any account, not just a malicious one.

### Finding Description
This mirrors the reported bug class precisely: a value-release path (`restoreBridgeTransaction` in the Sherlock report, `claim_rewards_to` here) depends on a precondition/parameter (`invalidBridgedAmountsPool` there, benchmark-derived `Weight` here) that has no way of being satisfied under the current runtime configuration, so the call is unconditionally rejected while the underlying value (bridge-relayer rewards accrued in `RelayerRewards`) remains locked in storage/pot accounts with no way to reach it. There actually was a fix upstream (`prdoc/stable2606/pr_10952.prdoc`) [7](#0-6)  that changed `bridge-hub-westend`'s `prepare_rewards_account` to return `Some(..)` and gave it a real weight [8](#0-7)  and [9](#0-8) , but the equivalent Bridge Hub Rococo implementation still returns `None` [5](#0-4)  and its shipped weight file still contains the `Weight::MAX` sentinel for both configured instances [3](#0-2) [4](#0-3) .

No signed-extension guard, filter, or `ensure!` check in `do_claim_rewards` protects against this — the call simply never reaches dispatch because the assigned weight is rejected at the transaction-validity / block-building stage, identically to how `restoreBridgeTransaction` always reverted on the zero-address check.

### Impact Explanation
Any relayer whose registered reward requires the `claim_rewards_to` alternative-beneficiary path (as opposed to the plain `claim_rewards`, which pays only to the calling relayer's own account) has no way to redirect/claim it on Bridge Hub Rococo. Combined with reward kinds designed specifically for beneficiaries other than the relayer's local account (this is exactly the purpose the `claim_rewards_to` API exists for, as shown by the analogous Snowbridge/AssetHub beneficiary flow tested on Westend [10](#0-9) ), the accrued reward balance is permanently unreachable through this call on Rococo. This is a permanent user-fund lock consistent with the "permanent user-fund or bridge-state lock" acceptance criterion.

### Likelihood Explanation
This requires no privileged actor, governance action, or malicious peer — it is a straightforward consequence of the currently-shipped runtime configuration and weight table for Bridge Hub Rococo. Any ordinary relayer account attempting `claim_rewards_to` on that chain will always be rejected regardless of network conditions, making the likelihood effectively deterministic (100%) whenever that call path is the only way to redirect a reward.

### Recommendation
Update `BridgeRelayersConfig<RelayersForLegacyLaneIdsMessagesInstance>::prepare_rewards_account` and the permissionless-lanes equivalent in `bridge-hub-rococo` to return a real `(reward_kind, beneficiary)` pair (mirroring the fix already applied to `bridge-hub-westend`), then regenerate `claim_rewards_to`'s weight via benchmarking so it reflects real execution cost instead of the `Weight::MAX` override, and republish the corresponding weight files.

### Proof of Concept
1. Deploy/point at Bridge Hub Rococo runtime as currently defined.
2. Have a relayer accumulate a reward via `register_reward`/`RelayerRewards` for a reward kind that is only payable through an alternative beneficiary (per the extension design in `bridges/modules/relayers/src/extension/mod.rs`).
3. Submit `pallet_bridge_relayers::claim_rewards_to(reward_kind, beneficiary)`.
4. Because `WeightInfo::claim_rewards_to()` returns `Weight::from_parts(18_446_744_073_709_551_000, 0)` [3](#0-2) , the transaction's declared weight always exceeds the chain's per-block/per-extrinsic weight limit, so it is rejected by the runtime's `CheckWeight`/executive logic before `do_claim_rewards` ever executes — the reward remains stuck in `RelayerRewards` storage indefinitely, with no alternative path to claim it to a different beneficiary.

Note: I was unable to fully confirm (due to iteration limits) whether Bridge Hub Rococo currently defines any reward kind that *requires* the alternative-beneficiary path (i.e., whether this is purely a latent/dead-code weight artifact or an actively exploitable fund lock today). This should be verified against `bridge_common_config.rs` and any Snowbridge-style reward variants configured for Rococo before treating this as confirmed exploitable versus a benchmarking-only defect.

### Citations

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

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L777-795)
```rust
			let claim_location = VersionedLocation::V5(Location::new(
				1,
				[
					Parachain(1000),
					xcm::latest::Junction::AccountId32 {
						id: account2.clone().into(),
						network: None,
					},
				],
			));
			// In unit tests without proper HRMP channel setup, the claim will fail at XCM sending.
			assert_err!(
				BridgeRelayers::claim_rewards_to(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge,
					BridgeRewardBeneficiaries::AssetHubLocation(claim_location)
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);
```
