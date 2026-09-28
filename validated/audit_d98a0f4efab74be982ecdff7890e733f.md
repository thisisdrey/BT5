### Title
Remaining underlying tokens are not returned to the user when `VPool.deposit` pulls less than `amount_` - ([File: contracts/VesperGateway.sol](contracts/VesperGateway.sol))

### Summary
`VesperGateway.deposit` pulls `amount_` of the vToken's underlying asset from the user and approves the full `amount_` to the Vesper pool. If `vToken_.deposit(amount_)` consumes less than the approved/pulled amount (e.g., a Vesper pool that only partially fills due to a deposit cap, or any external pool behavior where fewer tokens are taken), the unconsumed `underlying` tokens remain inside the `VesperGateway` contract. There is no refund path for the user — only the governor can recover them via `TokenHolder.sweep`. This is the same bug class as `BLVaultLido` approving `lpAmountOut` to `auraBooster` without returning the remainder.

### Finding Description [1](#0-0) 

```solidity
// contracts/VesperGateway.sol:49-64
IERC20 _underlying = IERC20(vToken_.token());
_underlying.safeTransferFrom(_msgSender, address(this), amount_);

_underlying.safeApprove(address(vToken_), 0);
_underlying.safeApprove(address(vToken_), amount_);
uint256 _balanceBefore = vToken_.balanceOf(address(this));
vToken_.deposit(amount_);
uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;
```

The contract measures the vToken delta correctly, but never measures the `underlying` delta consumed by `vToken_.deposit`. Any `underlying - consumed` remainder is left in the gateway. Contrast with `SmartFarmingManager.flashRepay`, which explicitly refunds the excess (`contracts/SmartFarmingManager.sol:134-137`), and `VesperGateway.withdraw`, which forwards the measured `underlying` delta to the user (lines 87-92) — `deposit` lacks the symmetric refund.

### Impact Explanation
User-supplied underlying collateral tokens can be permanently locked in `VesperGateway`. Recovery requires governor intervention via `sweep` (`_requireCanSweep` restricted to governor, lines 100-102), so funds are frozen at minimum and effectively lost for unprivileged users — matching the Medium severity of the source report.

### Likelihood Explanation
Depends on the external `VPool` semantics: Vesper pools that partially fill deposits (deposit caps/limit handling) leave a remainder. The `vToken_` argument is user-chosen, but `pool_.depositTokenOf(vToken_)` will revert for unregistered collaterals, so an attacker cannot abuse this against others — the harm falls on the caller who triggers a partial deposit. Likelihood is low-to-moderate; impact is bounded to the leftover amount.

### Recommendation
Measure the underlying balance delta around `vToken_.deposit` and refund the remainder:

```solidity
uint256 _underlyingBefore = _underlying.balanceOf(address(this));
vToken_.deposit(amount_);
uint256 _leftover = _underlying.balanceOf(address(this)) - (_underlyingBefore - amount_);
if (_leftover > 0) _underlying.safeTransfer(_msgSender, _leftover);
```

### Proof of Concept
Hardhat sketch: use a registered vToken wrapper (or a fork test against a real Vesper pool with a deposit cap). Call `VesperGateway.deposit(pool_, vToken_, amount_)` where the pool only consumes `amount_ - X`. Assert `_underlying.balanceOf(gateway) == X` and `balanceOf(user)` decreased by the full `amount_`, demonstrating the unrefunded remainder.

Note: this finding is conditional on `VPool.deposit` consuming less than requested; the standard Vesper `VPool` implementation reverts on exceeding capacity rather than partially filling, so the analog is weaker than the source report if all registered vTokens always consume the full amount.

### Citations

**File:** contracts/VesperGateway.sol (L44-65)
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
    }
```
