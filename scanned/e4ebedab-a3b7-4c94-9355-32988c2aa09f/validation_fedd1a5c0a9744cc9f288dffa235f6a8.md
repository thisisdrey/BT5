Based on my research, I found a genuine local analog to the SSRF pattern: `pallet_bridge_relayers::claim_rewards_to` lets a permissionless caller (any registered relayer) supply an arbitrary `Location` as the reward beneficiary, which is then blindly used as the destination inside a `DepositAsset` XCM instruction sent under `UnpaidExecution { check_origin: None }` combined with a forged `UniversalOrigin(GlobalConsensus(Ethereum))` / `ReserveAssetDeposited` — i.e., the pallet trusts a caller-supplied "address" to reach into an internal system location without any allow-listing, exactly like the SSRF report's blind trust of an attacker-supplied URL.

### Title
Unrestricted relayer-supplied `Location` beneficiary in Snowbridge reward payout allows minting reserve assets into arbitrary internal accounts/pallets - (File: `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
`PayAccountOnLocation::pay_reward` [1](#0-0)  builds and sends an XCM to AssetHub that fabricates a `ReserveAssetDeposited` of "Ethereum" WETH/ETH and then executes `DepositAsset { assets: AllCounted(1), beneficiary }`, where `beneficiary` is a `Location` fully controlled by the calling relayer via the public `claim_rewards_to` extrinsic [2](#0-1)  and the runtime's `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)` wrapper [3](#0-2) . No filtering restricts which `Location` values are acceptable beyond it decoding successfully as a `VersionedLocation`.

### Finding Description
`claim_rewards_to` is callable by any signed account holding accumulated relayer rewards [2](#0-1) . For `BridgeReward::Snowbridge`, the beneficiary must be `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)` [4](#0-3) , which is converted to a `Location` with only a version-compatibility check (`Location::try_from(account_location)`), and then passed straight into `PayAccountOnLocation::pay_reward` as the `beneficiary` for the `DepositAsset` instruction. The XCM emitted is:

```
UnpaidExecution { weight_limit: Unlimited, check_origin: None }
DescendOrigin(InboundQueueLocation)
UniversalOrigin(GlobalConsensus(Ethereum))
ReserveAssetDeposited(assets)
DepositAsset { assets: AllCounted(1), beneficiary }
``` [5](#0-4) 

`beneficiary` is never checked against any allow-list of "safe" account-only locations (e.g., `AccountId32` junctions under `Here`/local network). Because AssetHub's XCM barrier trusts messages arriving from the sibling BridgeHub (a trusted sibling parachain sending `UnpaidExecution`), the only remaining gate on where the newly-minted reserve asset lands is whatever `LocationToAccountId`/asset-transactor logic AssetHub uses to resolve `beneficiary` into a deposit target. This is the SSRF-equivalent: the "URL" (attacker-chosen `Location`) is handed unchecked to an internal trusted component that will "fetch"/credit a resource at that address, instead of restricting to a known-safe destination format (e.g., only local `AccountId32` locations, as is done for `RococoWestend` rewards via plain `AccountId` beneficiaries [6](#0-5) ).

### Impact Explanation
If AssetHub's asset-transactor/`LocationToAccountId` set (e.g. `HashedDescription<AccountId, DescribeFamily<DescribeAllTerminal>>` seen in other runtime configs [7](#0-6) ) can resolve arbitrary interior `Location`s (parachain sovereign accounts, pallet-derived accounts, or other locations reachable via `HashedDescription`) into concrete `AccountId`s, an attacker-controlled relayer can direct freshly reserve-minted "ETH" tokens into internal/system-derived accounts that were never intended to receive external bridge-originated value — bypassing the same "must resolve to a genuine, self-owned account" invariant that the `LocalAccount` variant explicitly restricts to actual relayer accounts. This breaks the "settle exactly once to the rightful beneficiary" guarantee for bridge reward payouts.

### Likelihood Explanation
The path is reachable by any unprivileged relayer that has accrued a non-zero Snowbridge reward (attainable simply by relaying at least one message) and requires no validator/relayer collusion, no governance action, and no leaked keys — only crafting a `VersionedLocation` value for the `claim_rewards_to` extrinsic. The only defense observed in the repository is the type-level rejection of `LocalAccount` for `Snowbridge` rewards [8](#0-7) ; there is no equivalent restriction on the *shape* of the `Location` accepted for `AssetHubLocation`.

### Recommendation
Restrict the accepted `beneficiary` `Location` in `PayAccountOnLocation`/`BridgeRewardBeneficiaries::AssetHubLocation` to a narrow, explicitly allow-listed shape (e.g., only `Location::new(0, [AccountId32 { .. }])` anchored to the calling relayer's own account, mirroring how `claim_rewards` binds the beneficiary to `relayer.into()`), rejecting any other interior junction pattern before constructing the outbound XCM.

### Proof of Concept
1. A relayer accrues a `BridgeReward::Snowbridge` reward via normal message relaying (`register_reward`) [9](#0-8) .
2. The relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation::V5(<arbitrary_location>)))` as shown working with a plain `AccountId32` in the existing test [10](#0-9) , but substituting `<arbitrary_location>` with a crafted interior `Location` (e.g. targeting a parachain sovereign or pallet-derived account instead of the relayer's own `AccountId32`).
3. `pay_reward` sends the `DepositAsset` XCM with that unchecked `beneficiary` to AssetHub [1](#0-0) , crediting the reserve-minted reward asset to whatever account the crafted `Location` resolves to — not necessarily one the relayer controls or that governance intended to receive such deposits.

**Uncertainty note:** I could not fully verify, from the indexed portion of the codebase, the exact `LocationToAccountId`/asset-transactor configuration used specifically by production AssetHub-Westend/Rococo when receiving this particular reward XCM from BridgeHub (only test/integration snippets were available), so I cannot confirm the precise set of internal accounts reachable this way. Confirming the full blast radius would require inspecting the live AssetHub runtime's `XcmConfig` and `LocationToAccountId` composition in a full checkout, which may benefit from a Devin session with complete repository access.

### Citations

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L72-140)
```rust
/// An enum representing the different types of supported beneficiaries.
#[derive(
	Clone, Debug, Decode, DecodeWithMemTracking, Encode, Eq, MaxEncodedLen, PartialEq, TypeInfo,
)]
pub enum BridgeRewardBeneficiaries {
	/// A local chain account.
	LocalAccount(AccountId),
	/// A beneficiary specified by a VersionedLocation.
	AssetHubLocation(VersionedLocation),
}

impl From<sp_runtime::AccountId32> for BridgeRewardBeneficiaries {
	fn from(value: sp_runtime::AccountId32) -> Self {
		BridgeRewardBeneficiaries::LocalAccount(value)
	}
}

/// Implementation of `bp_relayers::PaymentProcedure` as a pay/claim rewards scheme.
pub struct BridgeRewardPayer;
impl bp_relayers::PaymentProcedure<AccountId, BridgeReward, u128> for BridgeRewardPayer {
	type Error = sp_runtime::DispatchError;
	type Beneficiary = BridgeRewardBeneficiaries;

	fn pay_reward(
		relayer: &AccountId,
		reward_kind: BridgeReward,
		reward: u128,
		beneficiary: BridgeRewardBeneficiaries,
	) -> Result<(), Self::Error> {
		match reward_kind {
			BridgeReward::RococoWestend(lane_params) => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(account) => {
						bp_relayers::PayRewardFromAccount::<
							Balances,
							AccountId,
							LegacyLaneId,
							u128,
						>::pay_reward(
							&relayer, lane_params, reward, account,
						)
					},
					BridgeRewardBeneficiaries::AssetHubLocation(_) => Err(Self::Error::Other("`AssetHubLocation` beneficiary is not supported for `RococoWestend` rewards!")),
				}
			},
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
			}
		}
	}
}
```

**File:** bridges/primitives/relayers/src/lib.rs (L163-189)
```rust
impl<T, Relayer, LaneId, RewardBalance>
	PaymentProcedure<Relayer, RewardsAccountParams<LaneId>, RewardBalance>
	for PayRewardFromAccount<T, Relayer, LaneId, RewardBalance>
where
	T: frame_support::traits::fungible::Mutate<Relayer>,
	T::Balance: From<RewardBalance>,
	Relayer: Clone + Debug + Decode + Encode + Eq + TypeInfo,
	LaneId: Decode + Encode,
{
	type Error = sp_runtime::DispatchError;
	type Beneficiary = Relayer;

	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
}
```

**File:** cumulus/parachains/runtimes/people/people-westend/src/xcm_config.rs (L96-107)
```rust
pub type LocationToAccountId = (
	// The parent (Relay-chain) origin converts to the parent `AccountId`.
	ParentIsPreset<AccountId>,
	// Sibling parachain origins convert to AccountId via the `ParaId::into`.
	SiblingParachainConvertsVia<Sibling, AccountId>,
	// Straight up local `AccountId32` origins just alias directly to `AccountId`.
	AccountId32Aliases<RelayNetwork, AccountId>,
	// Here/local root location to `AccountId`.
	HashedDescription<AccountId, DescribeTerminus>,
	// Foreign locations alias into accounts according to a hash of their standard description.
	HashedDescription<AccountId, DescribeFamily<DescribeAllTerminal>>,
);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L39-61)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L63-88)
```rust
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
```
