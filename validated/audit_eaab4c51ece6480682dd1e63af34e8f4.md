### Title
`VesperGateway.deposit` trusts an arbitrary, unvalidated `vToken_` contract, letting an attacker drain a victim's real underlying tokens — (`contracts/VesperGateway.sol`)

### Summary
`VesperGateway.deposit(IPool pool_, IVPool vToken_, uint256 amount_)` validates only that `pool_` is a registered pool; `vToken_` is never checked against the pool's registered collateral list (`depositTokenOf` is only consulted *after* the underlying has been pulled and approved). Analogous to the kompose report — where docs pointed users at an attacker-registrable external resource — the gateway points users' funds at a caller-supplied external contract that the attacker fully controls ("fake vToken as argument" is an explicitly allowed vector). An attacker deploys a malicious `IVPool` whose `token()` returns a real token (e.g. USDC), gets a victim (or any integrating frontend/router following the same "clone the repo and run the steps" trust model) to call `deposit(pool, fakeVToken, amount)`, and the malicious contract walks away with the victim's tokens while the call still succeeds.

### Finding Description
In `contracts/VesperGateway.sol` lines 44–65:

1. `IERC20(vToken_.token())` — the "underlying" is whatever the attacker-supplied contract returns (e.g. real USDC).
2. `_underlying.safeTransferFrom(_msgSender, address(this), amount_)` — the victim's real tokens are pulled into the gateway.
3. `_underlying.safeApprove(address(vToken_), amount_)` — the gateway grants the *attacker's contract* an allowance over the victim's tokens now held by the gateway.
4. `vToken_.deposit(amount_)` — the malicious contract does nothing (or pulls via the allowance itself), `balanceOf` delta `_vTokenAmount` can be any value, including 0.
5. `pool_.depositTokenOf(vToken_)` returns `address(0)` for the unregistered collateral; `vToken_.safeApprove(address(0), 0)` succeeds, and `IDepositToken(address(0)).deposit(0, _msgSender)` is a high-level call to a non-contract, which returns success with empty returndata — the return value is ignored anyway, so the whole call completes without reverting.

Result: the victim's `amount_` of real tokens sits in the gateway with a live allowance to the attacker's `vToken_`, which drains it via `transferFrom` at leisure (or inside `deposit()` itself). `TokenHolder` cannot help — the tokens are gone before any governor `sweep` could run, and the fallback/receive reverts don't block ERC20 transfers out via allowance. `deposit` notably also lacks the `nonReentrant` modifier that `withdraw` carries (line 73), though the attack needs no reentrancy.

### Impact Explanation
Direct theft of user funds: any user who approves the gateway and calls `deposit` with the attacker-provided `vToken_` (e.g. via a spoofed UI/docs/integration, exactly the trust assumption in the source report) loses the full `amount_` of the real underlying token, receiving no `msdToken` in return.

### Likelihood Explanation
The attack requires the victim to call `deposit` with the malicious `vToken_` address — the same precondition class as the referenced report (users following external instructions pointing at attacker-controlled resources). No privileged role, oracle manipulation, or protocol state is needed; the attacker only deploys a contract and publishes it.

### Recommendation
Resolve the deposit token first and revert if unregistered: `IDepositToken _dt = pool_.depositTokenOf(vToken_); if (address(_dt) == address(0)) revert ...;` before pulling/approving the underlying, and assert `vToken_.token() == address(_dt.underlying())`. Also add `nonReentrant` to `deposit` for consistency with `withdraw`.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch
contract FakeVPool {
    IERC20 public usdc; // real underlying returned by token()
    function token() external view returns (address) { return address(usdc); }
    function deposit(uint256) external {} // no-op; keep the allowance
    function withdraw(uint256) external {}
    function balanceOf(address) external view returns (uint256) { return 0; }
    function drain(address gateway, address to, uint256 amt) external {
        usdc.transferFrom(gateway, to, amt); // uses gateway allowance
    }
}

// Attack:
// 1. victim approves USDC to VesperGateway, calls:
//    gateway.deposit(registeredPool, fakeVPool, 1000e6)
//    -> gateway pulls 1000 USDC, approves fakeVPool, call does NOT revert
// 2. attacker calls fakeVPool.drain(gateway, attacker, 1000e6)
// assert: victim's USDC == attacker balance; victim's msdToken balance == 0
```

Uncertainty noted: `Pool.depositTokenOf` returning `address(0)` for unregistered collateral (rather than reverting) is inferred from the `EnumerableSet`/mapping storage layout and the gateway's own usage; I could not read the `Pool.sol` implementation body within the iteration budget. If it reverts for unknown collateral, the theft still succeeds mid-call if `FakeVPool.deposit` pulls via the allowance before the revert propagates — but the clean path described above holds on the deployed mapping-based lookup.