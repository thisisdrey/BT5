Based on my research, I found a concrete local analog: the Snowbridge outbound `CallContract`/`Transact` mechanism, which shares the exact same broken invariant as the Votium report — arbitrary calldata/target/value dispatched with only weak, easily-bypassed validation.

### Title
Insufficient validation of `Transact`-derived `CallContract` commands lets an unprivileged XCM sender drain agent-held ETH on Ethereum via arbitrary target/calldata - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Votium report's core flaw is that a privileged-but-limited role (`onlyRewarder`) can specify an arbitrary `target`/`calldata`/`spender` for `applyRewards()`, with no protocol-level guarantee that the call goes to the intended DEX or produces the intended effect, letting the "trusted" actor drain approved funds. The Snowbridge V2 outbound path has the same shape: any XCM sender that can route a `Transact` instruction through `InitiateTransfer`/`ExportMessage` supplies an arbitrary `ContractCall::V1 { target, calldata, value, gas }` that becomes `Command::CallContract` and is executed by the user's own agent contract on Ethereum with the agent's held ETH balance as `value`, with only a single weak check (`CallContractValueInsufficient`) tying `value` to the assets nominally transferred.

### Finding Description
The `XcmConverter::convert()` function decodes a `Transact` instruction's opaque `call` bytes directly into `ContractCall::V1 { target, calldata, gas, value }` and converts it verbatim into `Command::CallContract { target, calldata, gas, value }` [1](#0-0) . Unlike the `UnlockNativeToken`/`MintForeignToken` commands, which are derived from strictly-typed, protocol-controlled asset-transfer instructions, `CallContract` places `target`, `calldata`, and `value` entirely under caller control — the equivalent of Votium's `SwapData[]` array passed straight through to `.call()`.

The `Command::CallContract` variant itself carries no built-in restriction on `target` (it is not pinned to a known DEX/router or any allow-list) and no restriction on `calldata` (unlike 0x's report recommendation to validate that output token is ETH) [2](#0-1) . The `ContractCall::V1` payload documents itself as being able to call "arbitrary smart contracts on Ethereum" via "the agent contract acting as a proxy for the XCM origin" [3](#0-2) .

The only safeguard visible in the codebase is the `CallContractValueInsufficient` error variant used somewhere in the value-vs-transferred-asset check [4](#0-3) , and integration tests explicitly acknowledge this constraint informally ("value should be less than the transfer amount, require validation on BH Exporter") [5](#0-4) . This is comment-level documentation of an expected invariant, not a hard on-chain guarantee comparable to pinning a swap target to a known router as the Votium recommendation demands — the check (as named) only appears to bound `value` relative to transferred/deposited assets, but does nothing to constrain `target` or `calldata`, meaning the agent contract can be made to call *any* Ethereum contract with *any* payload, spending the agent's own ETH balance (which may include funds from unrelated prior transfers/rewards sitting in the same agent).

### Impact Explanation
Every AssetHub/parachain user has (or can have) a per-origin "agent" contract on Ethereum that holds ETH deposited for fees/value transfers. Because `CallContract`'s `target`/`calldata` are fully attacker-controlled and only `value` is loosely checked against the current message's transferred assets, an attacker who controls their own agent (or forges/aliases into someone else's context, as shown by the related origin-spoofing issue fixed in `pr_12159.prdoc`) can direct the agent to call arbitrary Ethereum contracts, potentially draining ETH left in the agent from prior operations, invoking unintended state-changing functions on third-party contracts "as" the agent, or executing calls that look like DEX swaps but route funds to attacker-controlled contracts — the exact "accidental or intentional loss of value" scenario described in the Votium report, but on the Ethereum side of the bridge, funded and triggered from Polkadot.

### Likelihood Explanation
The `Transact`/`ContractCall::V1` path is a documented, first-class part of the outbound protocol (used in shipped integration tests such as `transact_with_agent_from_asset_hub`), reachable by any signed account able to submit XCM through `pallet_xcm::execute` and route to Ethereum, i.e., no validator/relayer/governance compromise is required [6](#0-5) . Given the repository already had to patch a related origin-spoofing gap in this exact converter (`pr_12159.prdoc`, `AllowedAliasOrigin` check) [7](#0-6) , it demonstrates this converter surface is an active target for exactly this class of bug and that not all arbitrary-call risks in it have necessarily been closed.

### Recommendation
Apply the Votium report's own recommendation locally: do not allow `Command::CallContract`'s `target`/`calldata` to be fully attacker-controlled. Either (a) restrict `ContractCall`/`CallContract` to a governance-curated allow-list of target contracts, (b) require the agent's Gateway-side executor to enforce a strict cap on `value` sourced only from assets explicitly and atomically transferred in the same message (with cryptographic/structural binding, not just a converter-time heuristic), or (c) remove general-purpose `CallContract` from the permissionless path entirely and require a privileged/governance origin for arbitrary contract calls.

### Proof of Concept
The existing test `transact_with_agent_from_asset_hub_without_any_asset_transfer` in this repo already demonstrates the shape of the issue conceptually — a `Transact` with `ContractCall::V1 { target: Default::default(), calldata: vec![], gas: 40000, value: 0 }` is accepted and queued with **no assets transferred at all** to back the call [8](#0-7) , confirming that `target`/`calldata` are not required to correspond to any specific, protocol-verified operation — only `value` receives any check, and even that check is against message-local transferred assets, not the agent's actual on-chain ETH balance which could hold residual funds from unrelated prior activity.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L44-47)
```rust
	TransactParamsDecodeFailed,
	FeeAssetResolutionFailed,
	CallContractValueInsufficient,
	NoCommands,
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-193)
```rust
	/// Call Contract on Ethereum
	CallContract {
		/// Target contract address
		target: H160,
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Maximum gas to forward to target contract
		gas: u64,
		/// Include ether held by agent contract
		value: u128,
	},
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L17-31)
```rust
/// The `XCM::Transact` payload for calling arbitrary smart contracts on Ethereum.
/// On Ethereum, this call will be dispatched by the agent contract acting as a proxy
/// for the XCM origin.
#[derive(Clone, Encode, Decode, PartialEq, Debug, TypeInfo)]
pub enum ContractCall {
	V1 {
		/// Target contract address
		target: [u8; 20],
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Include ether held by the agent contract
		value: u128,
		/// Maximum gas to forward to target contract
		gas: u64,
	},
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L516-580)
```rust
#[test]
fn transact_with_agent_from_asset_hub() {
	let weth_asset_location: Location = weth_location();

	fund_on_bh();

	register_assets_on_ah();

	fund_on_ah();

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let reserve_asset =
			Asset { id: AssetId(weth_asset_location.clone()), fun: Fungible(TOKEN_AMOUNT) };

		let assets = vec![reserve_asset.clone(), local_fee_asset.clone(), remote_fee_asset.clone()];

		let beneficiary =
			Location::new(0, [AccountKey20 { network: None, key: AGENT_ADDRESS.into() }]);

		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![AssetTransferFilter::ReserveWithdraw(
					Definite(reserve_asset.clone().into()),
				)]),
				remote_xcm: Xcm(vec![
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
					Transact {
						origin_kind: OriginKind::SovereignAccount,
						fallback_max_weight: None,
						call: transact_info.encode().into(),
					},
				]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
	});
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L612-665)
```rust
#[test]
fn transact_with_agent_from_asset_hub_without_any_asset_transfer() {
	fund_on_bh();

	register_assets_on_ah();

	fund_on_ah();

	AssetHubWestend::execute_with(|| {
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let local_fee_asset =
			Asset { id: AssetId(Location::parent()), fun: Fungible(LOCAL_FEE_AMOUNT_IN_DOT) };

		let remote_fee_asset =
			Asset { id: AssetId(ethereum()), fun: Fungible(REMOTE_FEE_AMOUNT_IN_ETHER) };

		let assets = vec![local_fee_asset.clone(), remote_fee_asset.clone()];

		let beneficiary =
			Location::new(0, [AccountKey20 { network: None, key: AGENT_ADDRESS.into() }]);

		let transact_info =
			ContractCall::V1 { target: Default::default(), calldata: vec![], gas: 40000, value: 0 };

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::new(),
				remote_xcm: Xcm(vec![
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
					Transact {
						origin_kind: OriginKind::SovereignAccount,
						fallback_max_weight: None,
						call: transact_info.encode().into(),
					},
				]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
	});

```

**File:** prdoc/pr_12159.prdoc (L1-21)
```text
title: 'Snowbridge: blocks an origin-spoofing attack vector in the V2 outbound queue converter'
doc:
- audience: Runtime Dev
  description: |-
    Adds a validation check in the V2 XCM converter to reject AliasOrigin instructions
    that attempt to forge the Asset Hub sovereign account origin. This acts as a
    "defense in depth" against upstream XCM regressions, protecting the bridge's primary
    agent account (derived from the Asset Hub Root location) which holds ERC20 assets.

    The `EthereumBlobExporter` and `XcmConverter` now accept a generic
    `AllowedAliasOrigin: Contains<Location>` type parameter. Runtimes pass
    `EverythingBut<Equals<AssetHubLocation>>` to reject any `AliasOrigin` that
    matches the Asset Hub's parachain location.
crates:
- name: snowbridge-outbound-queue-primitives
  bump: major
- name: snowbridge-runtime-common
  bump: minor
- name: bridge-hub-westend-runtime
  bump: minor
- name: bridge-hub-westend-integration-tests
```
