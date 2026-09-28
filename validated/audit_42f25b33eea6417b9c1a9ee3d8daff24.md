### Title
Yield from rebasing collateral is permanently locked in `Treasury` since withdrawals are capped by `msdToken` supply - (File: contracts/Treasury.sol)

### Summary
`DepositToken` mints `msdTOKEN` 1:1 against the collateral amount measured at deposit time, and collateral can only leave `Treasury` through `Treasury.pull`, which burns an equal amount of `msdTOKEN` first. If the underlying collateral is a positive-rebasing token (balance grows over time), the excess balance in `Treasury` above `DepositToken.totalSupply()` can never be withdrawn — the same bug class as the Allo `poolAmount` finding.

### Finding Description
- `DepositToken.deposit` measures the actual received amount via a balance delta of the treasury and mints `msdTOKEN` only for that amount (`_mint(onBehalfOf_, _deposited)`). [1](#0-0) 
- `DepositToken._withdraw` burns `_withdrawn` msd tokens and calls `pool.treasury().pull(to_, _withdrawn)` — a user can never pull more underlying than msd tokens burned. [2](#0-1) 
- `Treasury.pull` is only callable by a registered `DepositToken` and transfers `underlying` 1:1 to the burned amount. There is no `skim`/`recoverFunds`/`sweep` on `Treasury` (it does not inherit `TokenHolder`, unlike `DepositToken` and `VesperGateway`). [3](#0-2) 
- The protocol's own invariant test encodes `depositToken.totalSupply() == underlying.balanceOf(treasury)`, confirming there is no intended mechanism for the treasury to hold more than supply. [4](#0-3) 
- `Treasury.claimFromVesper` can route out a surplus (`_amount -= _depositToken.totalSupply()`), but only for tokens returned by `IPoolRewards(vPool_).getRewardTokens()` — it does not help for a rebasing collateral that is not a Vesper reward token. [5](#0-4) 

So when a rebasing collateral (e.g., a stETH-style token whose `balanceOf` increases) is supported, `underlying.balanceOf(treasury) > depositToken.totalSupply()` and the delta is stuck forever.

### Impact Explanation
Permanent freezing of funds: the rebase yield accrued on pooled collateral is locked in `Treasury` with no public or privileged exit path (no `sweep`, `pull` requires burning supply that does not exist). If the rebase is ever negative, the last withdrawers are also left unredeemable, but the positive case alone matches the reported "increased portion locked" impact.

### Likelihood Explanation
Medium-low. It requires governance to list a positively-rebasing token as collateral (the codebase's `balanceBefore`/delta accounting shows non-standard ERC20s such as fee-on-transfer tokens were contemplated). No attacker action is needed — the lockup accrues automatically. Mitigable only if the collateral happens to be a Vesper reward token claimable via `claimFromVesper`.

### Recommendation
Add a governor-controlled `sweep`/`recoverFunds` on `Treasury` (e.g., inherit `TokenHolder` and restrict `_requireCanSweep` to governor), or an explicit `skim` that transfers `underlying.balanceOf(this) - depositToken.totalSupply()` to `feeCollector`. Alternatively, document and enforce that rebasing tokens must never be registered as collateral.

### Proof of Concept
Foundry-style PoC using a mock rebasing ERC20 whose `balanceOf` grows via a `rebase(percent)` hook:

```solidity
// test/foundry/poc/RebaseLock.t.sol
function test_rebaseYieldLocked() public {
    // rebasingCollateral: MockRebaseToken with `rebase(int256)` multiplying all balances
    deal(address(rebasing), alice, 1000e18);
    vm.startPrank(alice);
    rebasing.approve(address(depositToken), type(uint256).max);
    depositToken.deposit(1000e18, alice);   // mints 1000 msdTOKEN
    vm.stopPrank();

    // underlying balance in Treasury grows 10% (positive rebase)
    rebasing.rebase(10);                    // treasury: 1100, totalSupply: 1000

    // Alice withdraws everything she can
    vm.prank(alice);
    depositToken.withdraw(depositToken.balanceOf(alice), alice);

    assertEq(depositToken.totalSupply(), 0);
    // ~100 tokens (rebase yield) remain in Treasury with no exit path
    assertGt(rebasing.balanceOf(address(treasury)), 0);
    // no function exists to recover it:
    // vm.expectRevert(); treasury.sweep(...) -- function does not exist
    // treasury.pull is only callable by a registered DepositToken
    vm.expectRevert(SenderIsNotDepositToken.selector);
    treasury.pull(address(this), rebasing.balanceOf(address(treasury)));
}
```

The stuck balance can only move via `migrateTo` into another `Treasury` with the same limitation — the funds are never releasable to users or the fee collector.

### Citations

**File:** contracts/DepositToken.sol (L225-234)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);
```

**File:** contracts/DepositToken.sol (L545-552)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

```

**File:** contracts/Treasury.sol (L66-72)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
    }
```

**File:** contracts/Treasury.sol (L89-99)
```text
            uint256 _amount = _token.balanceOf(address(this));

            // Note: If the reward token is a collateral, transfer the surpass balance only
            IDepositToken _depositToken = _pool.depositTokenOf(_token);
            if (address(_depositToken) != address(0)) {
                _amount -= _depositToken.totalSupply();
            }

            if (_amount > 0) {
                _token.safeTransfer(to_, _amount);
            }
```

**File:** test/foundry/DepositToken.invariants.t..sol (L98-100)
```text
    function invariant_treasuryBalance() public {
        assertEq(depositToken.totalSupply(), underlying.balanceOf(address(treasury)));
    }
```
