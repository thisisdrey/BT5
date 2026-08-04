### Title
Unbounded `value` in Snowbridge V2 `Transact`/`CallContract` command lets a message drain ether accumulated in the destination agent contract via arbitrary target/calldata - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Connext report shows that when a protocol lets a caller supply an arbitrary destination and calldata for an intermediary contract that already holds funds (and grants allowances/uses its own balance), the caller can direct execution to steal whatever excess value sits in that intermediary. `pallet_xcm::execute`/`InitiateTransfer` on Asset Hub can embed a `Transact` instruction whose payload is `ContractCall::V1 { target, calldata, gas, value }`. The BridgeHub `XcmConverter::convert` in `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs` decodes this payload unconditionally into `Command::CallContract { target, calldata, gas, value }` without validating `value` against the assets actually reserved/transferred in the same message.

### Finding Description
`XcmConverter::convert()` [1](#0-0)  extracts a `Transact` instruction, decodes it as `ContractCall::V1 { target, calldata, gas, value }`, and pushes it straight into `Command::CallContract { target, calldata, gas, value }` with no cross-check against the ENA (ether) amount that was actually withdrawn/reserved earlier in the same XCM (`extract_ethereum_native_assets`, lines 120-154). The `value` field is documented as "Include ether held by agent contract" [2](#0-1) , i.e. it is explicitly meant to draw on ether the agent contract already possesses (from previous relayer rewards, unclaimed dust, or other users' transfers to the same agent), not only the amount attached to the current message.

On the Ethereum side, this command is dispatched by "the agent contract acting as a proxy for the XCM origin" and forwards `target`/`calldata`/`gas`/`value` — this is functionally identical to `Executor.sol`'s `execute()`: an intermediary contract that holds pooled/leftover value and grants a fully attacker-controlled `(target, calldata)` triple to perform an arbitrary external call, including ERC20 `approve` to self, `transfer`, or draining ether. Because `AgentIdOf::convert_location` derives the agent from the *origin consensus system* (e.g. a parachain), not per-transaction, one user's `AliasOrigin` can address a shared agent whose balance was funded by unrelated messages (relayer rewards, other users' PayFees/remote fee residues, or previous `CallContract` value left over). The integration test itself flags this as an acknowledged but unenforced invariant: `"value should be less than the transfer amount, require validation on BH Exporter"` [3](#0-2)  — yet `convert.rs` contains no such check.

A second edge-case test even demonstrates that a fully attacker-chosen `target`/`calldata` pair for `ContractCall::V1` can reach the outbound pipeline via a forged/aliased origin, only rejected there because of an unrelated `ClearOrigin` origin-leak defect, not because `target`/`calldata`/`value` are validated [4](#0-3) .

### Impact Explanation
If the Ethereum `Gateway`/agent contracts execute `CallContract` commands using the caller-supplied `value` against the agent's actual (pooled) ether balance rather than strictly the ether committed in that specific message, an unprivileged user can construct an XCM whose `Transact` payload sets `target` to any address and `calldata` to any bytes (e.g., an ERC20 `approve(attacker, type(uint256).max)` on a token the agent holds, or a call that simply captures leftover ether), draining value that belongs to other users/relayers accumulated in the shared agent contract. This is a theft-of-unbacked-value / fund-loss impact matching the "theft or unbacked mint or unlock" and "public underpriced work" categories for Snowbridge BridgeHub scope.

### Likelihood Explanation
Any signed account on Asset Hub can submit `pallet_xcm::execute` with `preserve_origin: true` and craft the `remote_xcm` themselves (as shown by the existing `transact_with_agent_from_asset_hub` test which already exercises exactly this code path with a non-zero `value` unrelated to a checked cap). No relayer, validator, or governance action is required — only the BridgeHub converter's decode/dispatch logic and, unverified from this repo, the Ethereum-side agent contract's handling of `value`, which is outside the polkadot-sdk repo and could not be directly inspected here.

### Recommendation
In `XcmConverter::convert()`, before pushing `Command::CallContract`, enforce that `value <= total ether (ENA) amount reserved/withdrawn earlier in this same message` (mirroring the check the code comment in the test already expects), and/or require the receiving agent contract to track and cap the per-message spendable value strictly to funds delivered by that specific message rather than the agent's cumulative balance.

### Proof of Concept
Using the existing test scaffolding in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs::transact_with_agent_from_asset_hub`, submit an XCM identical in shape but where `ContractCall::V1.value` is set larger than the ether actually reserved/transferred in the message (e.g. equal to the agent's known pre-existing balance from earlier relayer-reward residues), with `target`/`calldata` pointing at an ERC20 `approve(attacker, MAX)` or a direct ether-drain call. Because `convert.rs` performs no bound check on `value` relative to the message's own reserved ENA, the resulting `Command::CallContract` is queued and committed identically to the legitimate case, exporting an outbound command that (per its documented semantics) authorizes spending ether "held by the agent contract" beyond what this message actually funded — note the exact behavior of the Ethereum-side Gateway/agent contract that consumes this command is outside this repository and was not independently verified here.

### Citations

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L543-549)
```rust
		let transact_info = ContractCall::V1 {
			target: Default::default(),
			calldata: vec![],
			gas: 40000,
			// value should be less than the transfer amount, require validation on BH Exporter
			value: 4 * (TOKEN_AMOUNT - REMOTE_FEE_AMOUNT_IN_ETHER) / 5,
		};
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L457-491)
```rust
		let arbitrary_agent_call = ContractCall::V1 {
			target: ETHEREUM_DESTINATION_ADDRESS,
			calldata: vec![0xde, 0xad, 0xbe, 0xef],
			value: 0,
			gas: 100_000,
		};

		let assets = vec![local_fee_asset.clone(), remote_fee_asset.clone()];
		let forged_xcm = Xcm(vec![
			WithdrawAsset(assets.into()),
			PayFees { asset: local_fee_asset },
			// Clear the origin register to None. Under the logic flaw in the XCM executor's
			// InitiateTransfer implementation (with preserve_origin: true), this causes the
			// executor to export the message without prepending any origin-altering instructions.
			// Details: https://forum.polkadot.network/t/postmortem-xcm-initiatetransfer-origin-leak/17357
			ClearOrigin,
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![]),
				remote_xcm: Xcm(vec![
					AliasOrigin(forged_assethub_origin),
					DepositAsset { assets: Wild(AllCounted(0)), beneficiary: beneficiary() },
					Transact {
						origin_kind: OriginKind::Xcm,
						call: arbitrary_agent_call.encode().into(),
						fallback_max_weight: None,
					},
					SetTopic([9u8; 32]),
				]),
			},
		]);
```
