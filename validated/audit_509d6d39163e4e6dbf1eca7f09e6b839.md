## Analysis

The Keep3r report's core invariant is: **a public "generic" entrypoint hard-codes/assumes an interface that is incompatible with one of the variants the system now supports, so calls for that variant always revert, silently breaking the entrypoint's core function for that class of caller.** I found a structurally identical pattern in `pallet-bridge-relayers`.

### Title
`claim_rewards` always reverts for `BridgeReward::Snowbridge` relayers because it hard-codes a `LocalAccount` beneficiary that the payment procedure explicitly rejects - ([File: bridges/modules/relayers/src/lib.rs])

### Summary
The generic reward-claim extrinsic `claim_rewards` unconditionally converts the caller's `AccountId` into the pallet's configured `Beneficiary` type via `Into`/`From`. On BridgeHub Westend, `BeneficiaryOf<T,I>` is `BridgeRewardBeneficiaries`, whose `From<AccountId>` impl always produces `BridgeRewardBeneficiaries::LocalAccount(..)`. But `BridgeRewardPayer::pay_reward` explicitly rejects `LocalAccount` when `reward_kind` is `BridgeReward::Snowbridge`, returning `Error::Other("LocalAccount beneficiary is not supported for Snowbridge rewards!")`. Consequently, any relayer holding a registered Snowbridge reward who calls the "default"/primary `claim_rewards` extrinsic will always have the call fail — exactly like the Keep3r relay calling the deprecated `worked(address)` signature that the sidechain contract permanently rejects. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`claim_rewards` (call_index 0) is the generic/primary way relayers withdraw accumulated rewards:
```rust
pub fn claim_rewards(origin: OriginFor<T>, reward_kind: T::Reward) -> DispatchResult {
    let relayer = ensure_signed(origin)?;
    Self::do_claim_rewards(relayer.clone(), reward_kind, relayer.into())
}
```
`relayer.into()` invokes `BridgeRewardBeneficiaries::from(AccountId32)`, which is hard-wired to always build `LocalAccount(value)` — there is no branch that produces `AssetHubLocation(..)` for the caller.

`do_claim_rewards` then calls `T::PaymentProcedure::pay_reward(relayer, reward_kind, reward_balance, beneficiary)` inside a `try_mutate_exists`. For `BridgeReward::Snowbridge`, `BridgeRewardPayer::pay_reward` matches on the beneficiary and unconditionally errors out for `LocalAccount`:
```rust
BridgeReward::Snowbridge => match beneficiary {
    BridgeRewardBeneficiaries::LocalAccount(_) =>
        Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
    BridgeRewardBeneficiaries::AssetHubLocation(account_location) => { ... }
}
```
Because `claim_rewards` can *only* ever construct `LocalAccount`, it is structurally impossible for `claim_rewards` to ever succeed for a `BridgeReward::Snowbridge` reward kind — the exact "deprecated/incompatible interface always reverts" pattern from the Keep3r report, where the relay's hard-coded `worked(address)` selector can never match the sidechain contract's required `worked(address,uint256)` signature.

This is confirmed by the codebase's own regression test, which documents the always-revert behavior as expected: [4](#0-3) 

and again in the runtime test suite: [5](#0-4) 

### Impact Explanation
Any off-chain relayer tooling, wallet, or script that (reasonably, since it is call_index 0 and the "default" claim call) invokes `claim_rewards` for a Snowbridge-earned reward will have every such transaction fail with `FailedToPayReward`, burning transaction fees with no possibility of success through that path. This directly mirrors the reported bug class: a public dispatch wrapper that is incompatible with one deployed variant of the underlying accounting logic and therefore always reverts for that class of caller, degrading relayer reward processing for the Snowbridge lane specifically. Because `do_claim_rewards` uses `try_mutate_exists`, the storage mutation (and the `take()` of the reward) is rolled back on `Err`, so the reward itself is not permanently lost — a caller can still recover funds by using the correct `claim_rewards_to` extrinsic with an `AssetHubLocation` beneficiary. The impact is therefore a functionality break (broken core reward-claim entrypoint for a whole reward class) rather than a fund-loss or fund-lock bug.

### Likelihood Explanation
High likelihood of being hit in practice: `claim_rewards` is call_index 0, the most naturally discoverable/primary extrinsic for claiming rewards, and nothing in its signature or documentation indicates it is incompatible with `BridgeReward::Snowbridge`. Any unprivileged relayer that accumulated a Snowbridge reward and calls this entrypoint (rather than the newer, non-default `claim_rewards_to`) will deterministically hit this failure on every attempt, with no governance, malicious actor, or off-chain assumption required to trigger it.

### Recommendation
Either (a) make `BridgeRewardPayer::pay_reward` accept a `LocalAccount` beneficiary for `BridgeReward::Snowbridge` by internally converting the local `AccountId` into the equivalent `AssetHubLocation`/XCM beneficiary, so `claim_rewards` remains usable for all reward kinds, or (b) have the pallet's generic `claim_rewards` call fail fast with a clear, documented error (or be restricted/deprecated) for reward kinds that require an alternative beneficiary, and update relayer-facing tooling/documentation to always use `claim_rewards_to` for Snowbridge rewards.

### Proof of Concept
1. Register a `BridgeReward::Snowbridge` reward for `relayer` via `BridgeRelayers::register_reward(relayer, BridgeReward::Snowbridge, reward_amount)`.
2. Call `BridgeRelayers::claim_rewards(RuntimeOrigin::signed(relayer), BridgeReward::Snowbridge)`.
3. Observe the call fails with `pallet_bridge_relayers::Error::FailedToPayReward`, as shown by the existing test `bridge_rewards_works`: [6](#0-5) 
4. Confirm the reward remains registered and unclaimed (the `try_mutate_exists` rollback preserves `RelayerRewards`), and only `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation(..)` succeeds, per `claim_snowbridge_rewards_to_local_account_fails`/`claim_rewards_works`: [7](#0-6)

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L112-119)
```rust
		/// Claim accumulated rewards.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::claim_rewards())]
		pub fn claim_rewards(origin: OriginFor<T>, reward_kind: T::Reward) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer.clone(), reward_kind, relayer.into())
		}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L83-87)
```rust
impl From<sp_runtime::AccountId32> for BridgeRewardBeneficiaries {
	fn from(value: sp_runtime::AccountId32) -> Self {
		BridgeRewardBeneficiaries::LocalAccount(value)
	}
}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-120)
```rust
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L25-103)
```rust
#[test]
fn claim_rewards_works() {
	let assethub_location = BridgeHubWestend::sibling_location_of(AssetHubWestend::para_id());
	let assethub_sovereign = BridgeHubWestend::sovereign_account_id_of(assethub_location);

	let relayer_account = BridgeHubWestendSender::get();
	let reward_address = AssetHubWestendReceiver::get();

	BridgeHubWestend::fund_accounts(vec![
		(assethub_sovereign.clone(), INITIAL_FUND),
		(relayer_account.clone(), INITIAL_FUND),
	]);
	set_up_eth_and_dot_pool();

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <BridgeHubWestend as Chain>::RuntimeOrigin;
		let reward_amount = ETHER_MIN_BALANCE * 2; // Reward should be more than Ether min balance

		type BridgeRelayers = <BridgeHubWestend as BridgeHubWestendPallet>::BridgeRelayers;
		BridgeRelayers::register_reward(
			(&relayer_account.clone()).into(),
			BridgeReward::Snowbridge,
			reward_amount,
		);

		// Check that the reward was registered.
		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
				},
			]
		);

		let relayer_location = Location::new(
			0,
			[Junction::AccountId32 { id: reward_address.clone().into(), network: None }],
		);
		let reward_beneficiary =
			BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation::V5(relayer_location));
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_ok!(result);

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				// Check that the pay reward event was emitted on BH
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardPaid { relayer, reward_kind, reward_balance, beneficiary }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
					beneficiary: *beneficiary == reward_beneficiary,
				},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			AssetHubWestend,
			vec![
				// Check that the reward was paid on AH
				RuntimeEvent::ForeignAssets(pallet_assets::Event::Deposited { asset_id, who: owner, .. }) => {
					asset_id: *asset_id == eth_location(),
					owner: *owner == reward_address.clone().into(),
				},
			]
		);
	})
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L105-152)
```rust
#[test]
fn claim_snowbridge_rewards_to_local_account_fails() {
	let assethub_location = BridgeHubWestend::sibling_location_of(AssetHubWestend::para_id());
	let assethub_sovereign = BridgeHubWestend::sovereign_account_id_of(assethub_location);

	let relayer_account = BridgeHubWestendSender::get();
	let reward_address = AssetHubWestendReceiver::get();

	BridgeHubWestend::fund_accounts(vec![
		(assethub_sovereign.clone(), INITIAL_FUND),
		(relayer_account.clone(), INITIAL_FUND),
	]);
	set_up_eth_and_dot_pool();

	BridgeHubWestend::execute_with(|| {
		type Runtime = <BridgeHubWestend as Chain>::Runtime;
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <BridgeHubWestend as Chain>::RuntimeOrigin;
		let reward_amount = ETHER_MIN_BALANCE * 2; // Reward should be more than Ether min balance

		type BridgeRelayers = <BridgeHubWestend as BridgeHubWestendPallet>::BridgeRelayers;
		BridgeRelayers::register_reward(
			&relayer_account.clone(),
			BridgeReward::Snowbridge,
			reward_amount,
		);

		// Check that the reward was registered.
		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
				},
			]
		);

		let reward_beneficiary = BridgeRewardBeneficiaries::LocalAccount(reward_address);
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_err!(result, FailedToPayReward::<Runtime, ()>);
	})
}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/tests/tests.rs (L768-776)
```rust
			// Local account claiming is not supported for Snowbridge
			assert_err!(
				BridgeRelayers::claim_rewards(
					RuntimeOrigin::signed(account2.clone()),
					BridgeReward::Snowbridge
				),
				pallet_bridge_relayers::Error::<Runtime, BridgeRelayersInstance>::FailedToPayReward
			);

```
