## Analysis

The BullvBear bug's core invariant is: **an operation is marked "settled" as soon as the asset is dispatched toward a recipient, without verifying the recipient actually received it — and once marked settled, there is no way to redirect or reclaim the stuck value.** The closest verifiable analog in this repository is in the Snowbridge relayer reward-claim flow between BridgeHub and AssetHub. [1](#0-0) 

`do_claim_rewards` removes the `RelayerRewards` entry (`maybe_reward.take()`) and commits that removal as soon as `T::PaymentProcedure::pay_reward(...)` returns `Ok(())`, then emits `RewardPaid`. [2](#0-1) 

For the `Snowbridge` reward kind, `PayAccountOnLocation::pay_reward` only validates that the XCM can be *sent* to AssetHub (`validate_send`, `charge_fees`, `XcmSender::deliver`) — it never confirms that the `DepositAsset` instruction actually executes successfully on AssetHub. "Success" here means "message was queued," not "beneficiary received the funds." [3](#0-2) 

The relayer freely supplies the `beneficiary: Location` via the public, unprivileged `claim_rewards_to` extrinsic (`BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)`), with no local check that the location resolves to an account able to actually receive the foreign WETH asset on AssetHub.

### Title
Snowbridge relayer reward claims are marked settled on XCM *send* success, not on remote *execution* success, permanently losing funds to an unreachable trap location if the beneficiary can't receive them - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`claim_rewards_to` lets any relayer choose an arbitrary AssetHub `Location` as beneficiary. `pay_reward` (via `PayAccountOnLocation`) only checks that the outbound XCM to AssetHub is *validated and delivered* into the local queue, then returns `Ok(())`. `do_claim_rewards` on BridgeHub then irreversibly deletes the `RelayerRewards` entry for that relayer/reward-kind. If the remote `DepositAsset` instruction subsequently fails on AssetHub (e.g. because the chosen beneficiary account cannot receive/hold the foreign WETH asset, lacks the ED, or the location otherwise fails to resolve to a valid depositable account), the reserve-deposited WETH is trapped on AssetHub under the XCM program's *origin* (derived from `UniversalOrigin(Ethereum)` + `DescendOrigin(InboundQueueLocation)`), not under any location the relayer controls or can sign for. The relayer's reward is gone forever: it is already erased from `RelayerRewards`, and it cannot construct a matching signed origin to run `pallet_xcm::claim_assets` against the trap.

### Finding Description
This is structurally identical to the BullvBear defect: an operation that moves value to a recipient is treated as fully settled the moment the *sending/dispatch* step succeeds, while the actual receipt by the intended beneficiary is unverified and unguarded. In BullvBear, `settleContract()` committed the trade and stored `withdrawableCollectionTokenId[bull] = tokenId` when `safeTransferFrom` to the bull failed, but there was no way to redirect that entry to a different, NFT-capable address — the bull's asset was permanently stuck.

Here:
1. `claim_rewards_to(origin, reward_kind, beneficiary)` is public and unprivileged — only `ensure_signed` is required. [4](#0-3) 
2. `do_claim_rewards` takes the stored reward balance out of storage and treats `pay_reward()` returning `Ok(())` as final settlement, committing the removal via `try_mutate_exists`. [5](#0-4) 
3. `PayAccountOnLocation::pay_reward` builds an XCM containing `ReserveAssetDeposited` + `DepositAsset { beneficiary, .. }`, and only checks `validate_send`, `charge_fees`, and `deliver` — none of which confirm remote execution outcome. [6](#0-5) 
4. On AssetHub, if `DepositAsset` fails for the supplied beneficiary, the XCM executor traps the remaining holding register under the program's execution origin — a fixed, system-derived location representing the inbound-queue/Ethereum descend chain — not the relayer's account.

The codebase's own history confirms this exact bug class already surfaced once for inbound messages: the default/fallback asset claimer used the wrong network tag and became "effectively unrecoverable without a runtime upgrade" until fixed. [7](#0-6)  That fix only covers the *inbound-message* fallback claimer; it does not add remote-execution confirmation, retry, or reclaim capability to the **relayer reward-claim** path.

### Impact Explanation
An unprivileged relayer can permanently lose their earned Snowbridge reward with a single malformed/incompatible `beneficiary` argument to `claim_rewards_to`. Because the local `RelayerRewards` entry is deleted unconditionally once the XCM is merely *sent*, there is no retry, no error surfaced back to the relayer post-hoc, and the trapped WETH lands under an origin the relayer cannot reproduce as a signed account to run `claim_assets`. This is a permanent fund lock matching the "permanent user-fund or bridge-state lock" and "message queues... payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" impact categories.

### Likelihood Explanation
High for accidental loss (any malformed or incompatible `VersionedLocation`/wrong network id/non-existent account triggers it), and it is directly reachable by any signed account with a registered reward — no governance, no relayer collusion, no malicious peer assumption needed. The existing test suite already demonstrates failure paths for this exact payment procedure returning errors only for *local* send/fee failures, never validating remote deposit success. [8](#0-7) 

### Recommendation
Do not remove/finalize the `RelayerRewards` entry (or emit `RewardPaid`) purely on local XCM-send success. Options:
- Require a receipt/confirmation flow: keep the reward pending until an inbound confirmation from AssetHub (e.g. via a receipt XCM or a query-response) confirms the `DepositAsset` succeeded, then finalize.
- Anchor the trap-claimer of the reward-payout XCM program to a location derived from the relayer's own account (analogous to the AH inbound-queue fix in `pr_11919`), so a failed deposit traps funds under an origin the relayer can sign for and reclaim via `pallet_xcm::claim_assets`.
- Alternatively, validate/sanity-check the beneficiary `Location` locally (e.g., that it resolves to a depositable account class) before committing the storage removal.

### Proof of Concept
1. Relayer accrues a `Snowbridge` reward via `register_reward` (as in `claim_rewards_works`). [9](#0-8) 
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(bad_location))` where `bad_location` is a `Location` that cannot successfully receive the deposited WETH on AssetHub (e.g., points at a location whose `DepositAsset` conversion/deposit fails, unlike the working `relayer_location` used in the passing test).
3. On BridgeHub: `pay_reward` succeeds because `validate_send`/`charge_fees`/`deliver` all succeed (the message is merely queued); `do_claim_rewards` deletes the `RelayerRewards` entry and emits `RewardPaid`.
4. On AssetHub: the `DepositAsset` instruction fails; remaining WETH holding is trapped (`AssetsTrapped`) under the execution origin (`UniversalOrigin(Ethereum)` + `DescendOrigin(InboundQueueLocation)`), not under any location the relayer controls.
5. Relayer has no way to reconstruct a signed origin matching the trap key, and re-calling `claim_rewards_to` fails with `NoRewardForRelayer` because the entry was already removed in step 3 — the reward is permanently unrecoverable.

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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L358-400)
```rust
	#[test]
	fn pay_reward_fails_on_delivery() {
		#[derive(Default)]
		struct FailingDeliveryXcmSender;
		impl SendXcm for FailingDeliveryXcmSender {
			type Ticket = ();

			fn validate(
				_dest: &mut Option<Location>,
				_xcm: &mut Option<Xcm<()>>,
			) -> SendResult<Self::Ticket> {
				Ok(((), Assets::from(vec![])))
			}

			fn deliver(_xcm: Self::Ticket) -> core::result::Result<XcmHash, SendError> {
				Err(SendError::NotApplicable)
			}
		}

		type FailingDeliveryPayAccount = PayAccountOnLocation<
			MockRelayer,
			u128,
			EthereumNetwork,
			AssetHubLocation,
			InboundQueueLocation,
			FailingDeliveryXcmSender,
			MockXcmExecutor,
			MockCall,
		>;

		let relayer = MockRelayer(AccountId32::new([4u8; 32]));
		let beneficiary = Location::new(1, Here);
		let reward = 123u128;
		let result = FailingDeliveryPayAccount::pay_reward(&relayer, (), reward, beneficiary);

		assert!(result.is_err());
		let err_str = format!("{:?}", result.err().unwrap());
		assert!(
			err_str.contains("xcm send failure"),
			"Expected 'xcm delivery failure', got {:?}",
			err_str
		);
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
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
```

**File:** prdoc/stable2603-3/pr_11919.prdoc (L1-19)
```text
title: 'Snowbridge: Set default asset claimer to local network'
doc:
- audience: Runtime Dev
  description: |-
    The inbound-queue v2 message converter falls back to the Snowbridge sovereign
    account on AssetHub as the asset claimer when no explicit claimer is supplied.
    Previously this fallback used `AccountId32 { network: None, .. }`, which did
    not match the location AssetHub's signed-origin converter produces (it sets
    `network: Some(LocalNetwork)`). The trap-key hash stored on `AssetsTrapped`
    therefore could not be matched by a signed `polkadotXcm.claim_assets` call,
    making default-claimer trapped funds effectively unrecoverable without a
    runtime upgrade.

    This PR sets `network: Some(LocalNetwork::get())` on the fallback claimer so
    its `Location` agrees with what `SignedToAccountId32<_, _, LocalNetwork>`
    yields on AssetHub, and adds a test covering the no-claimer-supplied path.
crates:
- name: snowbridge-inbound-queue-primitives
  bump: patch
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L25-61)
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
```
