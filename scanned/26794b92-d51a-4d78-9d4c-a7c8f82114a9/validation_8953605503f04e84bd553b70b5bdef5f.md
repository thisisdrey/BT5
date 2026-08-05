### Title
Unvalidated `value` field in `Transact`/`ContractCall::V1` allows draining an Ethereum agent's Ether balance beyond what is reserved in the message - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs`)

### Summary
The Snowbridge V2 outbound converter builds a `Command::CallContract { target, calldata, gas, value }` from a user-supplied `Transact`/`ContractCall::V1` XCM instruction without ever validating that `value` (the amount of ether the Gateway contract will pull "held by the agent contract" and forward to `target`) is bounded by the ether actually reserved/withdrawn for that specific message. This is the same broken invariant as the Tapioca `mTOFT` bug: a value-holding contract (the per-origin Ethereum agent, analogous to `mTOFT`) can be drained through an unrelated public-dispatch path (`CallContract`, analogous to `sendPacket`'s `lzNativeGasDrop`) because the amount field is never bound to what was actually escrowed for the operation.

### Finding Description
In `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:217-325`), the converter:
1. Extracts the reserved ether fee (`extract_remote_fee`) and any ENA/PNA transfer amounts from `WithdrawAsset`/`ReserveAssetDeposited` instructions.
2. Independently decodes an optional `Transact` instruction into a `ContractCall::V1 { target, calldata, gas, value }` and pushes it straight into `commands` as `Command::CallContract { target, calldata, gas, value }` (lines 294-305), with **no check that `value` is less than or equal to any amount transferred/reserved in the same message**.

Crucially, the file defines the error variant `CallContractValueInsufficient` (line 46) — clearly intended for exactly this validation — but it is **never referenced or raised anywhere** in `convert()`. This is dead code that documents an intended-but-missing guard.

On Ethereum, `Command::CallContract`'s `value` is documented as "Include ether held by the agent contract" (`bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs:182-192`), i.e. the Gateway will forward up to `value` wei of the agent's own accumulated Ether balance to an arbitrary `target` when executing this command — not just ether newly bridged in this message. Because the converter never checks `value` against the amount actually withdrawn/reserved for this XCM, any origin capable of producing a valid `AliasOrigin` + `Transact` (an ordinary signed AssetHub/Penpal account routing through `InitiateTransfer`, as demonstrated by the existing test `transact_with_agent_from_asset_hub`) can set `value` to the agent's entire Ether balance and route it to an attacker-controlled `target` contract, stealing any Ether previously accumulated in that agent (e.g. leftover fees, prior partial transfers, or funds belonging to other unrelated messages that share the same agent).

The integration test at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:543-549` explicitly comments:
```
// value should be less than the transfer amount, require validation on BH Exporter
```
confirming this validation was expected to exist on the Bridge Hub exporter side, yet no such check is present in the shipped converter code.

### Impact Explanation
An agent contract on Ethereum can accumulate Ether across multiple bridging operations (fees, partial transfers, retries, `CallContract` value carried by prior commands). Because `Command::CallContract.value` is unbounded relative to the message's own reserved assets, any account that can route an XCM through the Snowbridge V2 exporter with a valid `AliasOrigin`/agent (which is a normal, unprivileged operation available to any parachain/account with an established agent) can drain the entire accumulated Ether balance of that agent to an arbitrary contract address on Ethereum. This is a direct theft of funds analogous to the `mTOFT` ETH theft — no relayer, validator, or governance compromise is required, only crafting a valid XCM message.

### Likelihood Explanation
High: constructing the XCM (`WithdrawAsset`/`PayFees`, `AliasOrigin`, `DepositAsset`, `Transact(ContractCall::V1{ value: <agent_balance> })`, `SetTopic`) is exactly the pattern already exercised by the repository's own test `transact_with_agent_from_asset_hub`, so no privileged capability is required beyond having an agent (which any parachain/account routing through the bridge already has). The dead `CallContractValueInsufficient` error variant is strong evidence the guard was intended but not wired in, meaning the "happy path" tests currently pass while the missing bound remains exploitable whenever an agent holds any nonzero residual Ether balance.

### Recommendation
In `XcmConverter::convert`, after extracting the reserved ether amount (`fee_amount` plus any ENA amount transferred to the ether location), enforce that the `Transact`'s `ContractCall::V1.value` does not exceed the ether amount actually reserved/withdrawn for this specific message, returning `XcmConverterError::CallContractValueInsufficient` (the already-defined but unused variant) otherwise. Alternatively, track and cap `CallContract.value` against a per-message escrow rather than the agent's total on-chain balance, so a single message can never authorize draining ether that was not part of that message's own transfer.

### Proof of Concept
1. An attacker with a registered agent on Ethereum (any account that has previously bridged assets through Snowbridge V2, so the agent has accumulated some Ether balance from fees/prior transfers) submits, via `pallet_xcm::execute`, an XCM identical in shape to the repo's own `transact_with_agent_from_asset_hub` test:
   ```
   WithdrawAsset(local_fee_asset)
   PayFees(local_fee_asset)
   InitiateTransfer {
       destination: ethereum(),
       remote_fees: ReserveWithdraw(small_remote_fee_asset),
       assets: [], // no real asset transfer needed
       remote_xcm: [
           AliasOrigin(attacker_origin),
           DepositAsset { assets: Wild(AllCounted(..)), beneficiary },
           Transact { call: ContractCall::V1 {
               target: attacker_contract,
               calldata: vec![],
               gas: 100_000,
               value: AGENT_ETHER_BALANCE, // set to the agent's full held balance, far exceeding remote_fee
           }.encode() },
           SetTopic(...),
       ],
   }
   ```
2. `XcmConverter::convert` (`bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:294-305`) decodes the `Transact` payload and unconditionally emits `Command::CallContract { value: AGENT_ETHER_BALANCE, .. }` since no check exists against `fee_amount` or reserved ENA amounts.
3. The Gateway contract on Ethereum executes `CallContract`, forwarding `AGENT_ETHER_BALANCE` wei of the agent's balance to `attacker_contract`, draining funds that were never part of the attacker's own reserved/withdrawn assets in this message. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-192)
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
