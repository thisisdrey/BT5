### Title
`VesperGateway.withdraw` burns the user's collateral claim before converting vTokens, forwarding whatever the Vesper pool returns with no minimum-out check - (File: contracts/VesperGateway.sol)

### Summary
The Illuminate bug class is: a user burns a claim token in exchange for a share of a backing pool whose conversion to the payout asset may be incomplete, so the burn is final but the payout is whatever the backing redemption yields at that moment. `VesperGateway.withdraw()` exhibits the same shape: it burns the user's `msdToken` collateral claim first, then unwraps the resulting vTokens through the external Vesper pool and forwards the balance delta to the user without any slippage/minimum-out protection.

### Finding Description
In `withdraw()`:

1. The user's `msdTokens` are pulled in via `safeTransferFrom` and burned through `_depositToken.withdraw(amount_, address(this))`, which internally calls `Treasury.pull` to move `_vTokenAmount` of vTokens to the gateway [1](#0-0) [2](#0-1) .
2. The gateway then calls `vToken_.withdraw(_vTokenAmount)` and computes the payout as the raw balance delta `_underlying.balanceOf(this) - _balanceBefore`, forwarding exactly that to the user [3](#0-2) .

Vesper `VPool.withdraw` can deliver less underlying than the shares' face value when the pool is short on liquid funds (strategies not yet divested, pool underwater, withdraw fees), and the codebase itself acknowledges this elsewhere — `AMO._withdrawFromVesper` comments "Vesper pool may withdraw less, of course burn less shares too, than requested" and uses balance deltas for that reason [4](#0-3) . Unlike `AMO`, the gateway has already destroyed the user's claim when the shortfall happens, and there is no `minAmountOut_` parameter and no post-condition check that `_underlyingAmount` covers `amount_`. As in the Illuminate issue, a user sees "maturity passed / pool exists," calls withdraw, and permanently locks in a loss that would have been recoverable by simply waiting for the Vesper pool to regain liquidity.

### Impact Explanation
Direct, permanent loss of user funds: the `msdToken` claim (which remains fully valuable — it is still counted as collateral backing debt positions and is redeemable 1:1 for vTokens via `DepositToken.withdraw`) is burned, while the underlying received can be arbitrarily less than `amount_`. The shortfall is not recreditable since the shares were consumed by the Vesper withdrawal.

### Likelihood Explanation
Requires no privileged action: any holder of a vToken-backed `msdToken` (e.g., vaUSDC, vaETH deposit tokens) can call `VesperGateway.withdraw` directly while the underlying Vesper pool is liquidity-constrained — a recurring, normal state for Vesper pools during utilization spikes or strategy drawdowns. The user harm is self-inflicted but unprotected, matching the accepted medium-severity precedent where "enforcing a specific order of contract calls off-chain is not secure."

### Recommendation
Add a `minUnderlyingOut_` parameter to `VesperGateway.withdraw` and `require(_underlyingAmount >= minUnderlyingOut_)` after the balance-delta measurement, reverting the whole transaction (which restores the msdTokens) if the Vesper pool underdelivers. Optionally, use `vToken_.withdraw` return/quote to pre-check deliverable liquidity.

### Proof of Concept
Foundry fork test outline: fork mainnet, take a registered `Pool` + a vToken `DepositToken` (e.g., vaUSDC), deposit underlying via `VesperGateway.deposit` to obtain `msdTokens`, then simulate a Vesper liquidity shortfall (e.g., by moving pool liquidity into strategies via an authorized Vesper rebalance on a historical block where `pool.token().balanceOf(pool) < withdrawable`, or by wrapping `vToken_.withdraw` behavior in a state where it pays out < requested). Call `gateway.withdraw(pool, vToken, amount)` and assert: (a) user's `msdToken` balance is reduced by `amount`, (b) `underlying.balanceOf(user)` increased by less than the oracle/`pricePerShare` value of `amount`, demonstrating irreversible loss with no revert path.

### Citations

**File:** contracts/VesperGateway.sol (L79-84)
```text
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        _depositToken.safeTransferFrom(_msgSender, address(this), amount_);

        // 2. Withdraw `vTokens` from `Synth`
        (uint256 _vTokenAmount, ) = _depositToken.withdraw(amount_, address(this));

```

**File:** contracts/VesperGateway.sol (L86-92)
```text
        IERC20 _underlying = IERC20(vToken_.token());
        uint256 _balanceBefore = _underlying.balanceOf(address(this));
        vToken_.withdraw(_vTokenAmount);
        uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;

        // 4. Transfer `underlying` to the `_msgSender()`
        _underlying.safeTransfer(_msgSender, _underlyingAmount);
```

**File:** contracts/DepositToken.sol (L536-553)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
```

**File:** contracts/AMO.sol (L141-147)
```text
    ) private returns (uint256) {
        // Vesper pool may withdraw less, of course burn less shares too, than requested.
        // Hence the difference of Synth balance after withdraw and before withdraw is actual Synth withdrawn.
        uint256 _before = syntheticToken_.balanceOf(address(this));
        vPool_.withdraw(shares_);
        return syntheticToken_.balanceOf(address(this)) - _before;
    }
```
