## Finding

### Title
`pallet-revive`'s EVM `DIFFICULTY`/`PREVRANDAO` opcode returns a fixed, chain-wide constant instead of real per-block entropy, defeating on-chain "randomness" used by ported Solidity contracts - ([File: substrate/frame/revive/src/vm/evm.rs])

### Summary
`pallet-revive` executes EVM bytecode (via `revm`) so that unmodified Solidity contracts can run on a Substrate chain. The `DIFFICULTY` opcode (post-merge `PREVRANDAO`) — the exact opcode class abused in the HoneyJar report (`block.difficulty`) — is hard-wired to a single constant value that never changes, is publicly known in the source code, and is identical for every block and every chain running this pallet. Any ported contract that reuses the common (if already weak) Solidity idiom of mixing `block.timestamp`/`block.difficulty`/`msg.sender` for pseudo-randomness loses the `block.difficulty` entropy component entirely on this runtime, making the exact "predict-then-retry" class of attack in the external report strictly easier to execute than on real Ethereum.

### Finding Description
`pallet-revive` defines: [1](#0-0) 

and the opcode handler simply pushes this constant onto the stack whenever a contract executes `DIFFICULTY`/`PREVRANDAO`: [2](#0-1) 

The opcode dispatch table wires `DIFFICULTY` straight to this handler with no per-block derivation: [3](#0-2) 

A dedicated test confirms the value is a static constant, not derived from chain randomness or block state: [4](#0-3) 

Notably, the project's own PR history shows this was noticed only from an RPC-consistency angle (the eth-rpc was returning `0` for block difficulty because it didn't know about this constant), not from a security angle — i.e., the implementation choice to hardcode `DIFFICULTY` was never evaluated against contracts that rely on it as an entropy source: [5](#0-4) 

This is a direct structural analog to the HoneyJar bug: the report's contract computed `keccak256(block.timestamp, block.difficulty, msg.sender)` and exploited the fact that `block.difficulty` is not meaningfully unpredictable within a transaction. On `pallet-revive`, `block.difficulty` isn't merely weak — it is a compile-time constant, `2500000000000000`, identical on every block of every chain that includes this pallet. Any contract ported to `pallet-revive` that includes `block.difficulty` as one of several randomness ingredients effectively loses that ingredient's entropy completely, collapsing its "randomness" to whatever remains (typically `block.timestamp` and `msg.sender`, both of which are attacker-observable/controllable at call time). This directly reproduces the report's root cause ("on-chain values used as randomness are predictable/attacker-influenced") but in a strictly worse form, because on `pallet-revive` the value is not just weak, it's globally and permanently known in advance (`0x8e1bc9bf040000` / `2500000000000000`).

### Impact Explanation
Contracts on EVM-compatible chains commonly (and, per many audits, incorrectly) treat `block.difficulty`/`PREVRANDAO` as a source of pseudo-randomness for reward/lottery/NFT-rarity logic exactly like the HoneyJar `Beekeeper` contract in the report. When such contracts are deployed unmodified on `pallet-revive` (its explicit design goal is Solidity-contract compatibility), the randomness scheme is degraded further than on real Ethereum: the `DIFFICULTY` component contributes zero variance across blocks and across chains, since it is a fixed literal in the runtime. This allows an unprivileged attacker to compute in advance any outcome that depends on `DIFFICULTY`, and to combine that with observation of the remaining (weak) entropy sources to reliably obtain favorable random outcomes for lotteries, jackpots, NFT trait/rarity assignment, or reward distribution built on top of ported contracts — i.e., unauthorized/forged acceptance of a "random" outcome leading to unbacked/incorrect payout or asset allocation, matching the "theft or unbacked mint", "duplicate settlement or payout", and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
Likelihood is high for any contract migrated to `pallet-revive` that uses `block.difficulty`/`PREVRANDAO` as part of its randomness formula (a widespread, well-documented anti-pattern that audit firms flag routinely, as shown by the very report used as the seed here). No privileged access, governance action, validator/collator collusion, or off-chain infrastructure is required — a normal user interacting with the ported contract through an ordinary transaction can exploit the deterministic constant directly. The bug is entirely in `pallet-revive`'s own opcode implementation, not a peer/validator assumption.

### Recommendation
Do not hardcode `DIFFICULTY`/`PREVRANDAO`. Derive it, if it must be supplied for EVM compatibility, from a legitimate on-chain randomness source (e.g., the runtime's `Randomness` trait / BABE `ParentBlockRandomness`) that varies per block and cannot be predicted by the caller at call time, and clearly document in `pallet-revive`'s compatibility notes that ported contracts must not rely on any single block-info opcode for security-critical randomness. At minimum, surface this as an explicit compatibility caveat so integrators porting existing audited Solidity contracts are aware `DIFFICULTY` carries no entropy on this chain, preventing silent security regressions like the one demonstrated in the HoneyJar report.

### Proof of Concept
1. Deploy (via `pallet-revive`) any Solidity contract equivalent to the HoneyJar `Beekeeper`, whose "random" jar-fermentation index is computed as `uint256(keccak256(abi.encodePacked(block.timestamp, block.difficulty, msg.sender)))`.
2. Query `DIFFICULTY` opcode result off-chain (or simply read the constant from `substrate/frame/revive/src/vm/evm.rs`): it is always `2500000000000000`.
3. Because `block.difficulty` is now a known constant, an attacker's helper contract only needs to predict the remaining `block.timestamp` (known before submission, since blocks tick at a fixed cadence and one can submit late in a slot) and its own address to fully precompute `keccak256(...)` before minting.
4. As in the report's `Reroll` contract, the attacker's contract implements a receive-hook that checks whether the precomputed index matches the desired reward slot and reverts the enclosing call if not, retrying in a subsequent block until it matches — except here it needs to defeat only the timestamp component, since the difficulty component contributes nothing to search over, materially reducing the attacker's work factor compared to the original report scenario. [4](#0-3)

### Citations

**File:** substrate/frame/revive/src/vm/evm.rs (L45-51)
```rust
/// Hard-coded value returned by the EVM `DIFFICULTY` opcode.
///
/// After Ethereum's Merge (Sept 2022), the `DIFFICULTY` opcode was redefined to return
/// `prevrandao`, a randomness value from the beacon chain. In Substrate pallet-revive
/// a fixed constant is returned instead for compatibility with contracts that still read this
/// opcode. The value is aligned with the difficulty hardcoded for PVM contracts.
pub(crate) const DIFFICULTY: u64 = 2500000000000000_u64;
```

**File:** substrate/frame/revive/src/vm/evm/instructions/block_info.rs (L66-73)
```rust
/// Implements the DIFFICULTY/PREVRANDAO instruction.
///
/// Pushes the block difficulty (pre-merge) or prevrandao (post-merge) onto the stack.
pub fn difficulty<E: Ext>(interpreter: &mut Interpreter<E>) -> ControlFlow<Halt> {
	interpreter.ext.charge_or_halt(EVMGas(BASE))?;
	interpreter.stack.push(U256::from(DIFFICULTY))?;
	ControlFlow::Continue(())
}
```

**File:** substrate/frame/revive/src/vm/evm/instructions/mod.rs (L102-107)
```rust
		BLOCKHASH => host::blockhash(interpreter),
		COINBASE => block_info::coinbase(interpreter),
		TIMESTAMP => block_info::timestamp(interpreter),
		NUMBER => block_info::block_number(interpreter),
		DIFFICULTY => block_info::difficulty(interpreter),
		GASLIMIT => block_info::gaslimit(interpreter),
```

**File:** substrate/frame/revive/src/tests/sol/block_info.rs (L199-218)
```rust
/// Tests that the difficulty opcode works as expected.
#[test_case(FixtureType::Solc)]
#[test_case(FixtureType::Resolc)]
fn difficulty_works(fixture_type: FixtureType) {
	let (code, _) = compile_module_with_type("BlockInfo", fixture_type).unwrap();
	ExtBuilder::default().build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
		let Contract { addr, .. } =
			builder::bare_instantiate(Code::Upload(code)).build_and_unwrap_contract();

		let result = builder::bare_call(addr)
			.data(BlockInfo::BlockInfoCalls::difficulty(BlockInfo::difficultyCall {}).abi_encode())
			.build_and_unwrap_result();
		let decoded = BlockInfo::difficultyCall::abi_decode_returns(&result.data).unwrap();
		assert_eq!(
			// Aligned with the value set for PVM (truncated to u64)
			DIFFICULTY as u64,
			decoded
		);
	});
```

**File:** prdoc/stable2512/pr_10186.prdoc (L1-12)
```text
title: Return the correct block difficulty from the eth-rpc
doc:
- audience: Runtime Dev
  description: |-
    # Description

    This PR fixes an issue in the eth-rpc/pallet-revive that was causing it to return an incorrect value for the block's difficulty or prevrandao.

    In the VM/interpreter implementation we use a constant for the block difficulty. However, the eth block construction side was unaware of this constant being used and therefore the RPC was always returning a block difficulty of zero.
crates:
- name: pallet-revive
  bump: patch
```
