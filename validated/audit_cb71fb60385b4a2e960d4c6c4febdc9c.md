### Title
Fee-on-transfer collateral creates unbacked `msdTOKEN` supply on withdrawal, insolventing late withdrawers — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken.deposit()` correctly measures the actual amount received by the `Treasury` via a balance-delta check, so fee-on-transfer collateral mints `msdTOKEN` only for what arrived. The withdrawal path is not symmetric: `DepositToken._withdraw()` burns the full `_withdrawn` amount of `msdTOKEN` and instructs `Treasury.pull()` to transfer `_withdrawn` underlying to the user. With a fee-on-transfer underlying, the recipient receives `_withdrawn - transferFee` while the full `_withdrawn` is burned, permanently draining the backing ratio. Each withdrawal leaks the fee amount of backing, so over time `underlying.balanceOf(treasury) < depositToken.totalSupply()`, and the last withdrawers' `pull()` reverts on insufficient balance — a DoS / freezing of funds identical in class to the Allo `poolAmount` over-accounting issue.

### Finding Description
In `contracts/DepositToken.sol:225-227`, deposits defensively measure the delta:

```solidity
uint256 _balanceBefore = _underlying.balanceOf(_treasury);
_underlying.safeTransferFrom(_msgSender, _treasury, amount_);
amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
```

But `contracts/DepositToken.sol:545-551` burns shares and pushes tokens without accounting for the fee taken on the outbound transfer:

```solidity
(_withdrawn, _fee) = quoteWithdrawOut(amount_);
...
_burn(account_, _withdrawn);
_pool.treasury().pull(to_, _withdrawn);
```

`Treasury.pull()` (`contracts/Treasury.sol:66-72`) does `underlying().safeTransfer(to_, amount_)`, so for a fee-on-transfer underlying the recipient gets less than `amount_` while `totalSupply` decreased by the full `amount_`.

### Impact Explanation
The solvency invariant `treasury.balanceOf(underlying) >= depositToken.totalSupply()` is violated: every withdraw burns more shares than the underlying that actually left the system as user value, but the shortfall stays inside the Treasury only if the token burns on the sender side; for typical fee-on-transfer tokens that divert to a fee address or burn from the transferred amount, the Treasury's balance drops by `amount_` while the user receives `amount_ - fee`. Either way, aggregated withdrawals leave the Treasury with less than the outstanding `msdTOKEN` supply. Once `balanceOf(treasury) < nextWithdrawAmount`, `Treasury.pull()` reverts, permanently freezing the last depositors' collateral.

### Likelihood Explanation
Requires a fee-on-transfer token to be a registered collateral underlying. That is a governance onboarding decision (which limits severity), but the codebase explicitly supports such tokens — the deposit delta-check and `test/DepositToken.test.ts:433-449` ("should deposit TOKEN and mint msdTOKEN when TOKEN has transfer fee") prove intended support. The deposit side being protected while the withdraw side is not is an inconsistent accounting asymmetry, not a mocked-only path.

### Recommendation
Either measure the actual amount delivered on withdrawal (have `Treasury.pull`/`_withdraw` compare `to_` balance before/after and reduce the burned or accounted amount accordingly), or explicitly reject fee-on-transfer underlyings at onboarding and document that assumption.

### Proof of Concept
Foundry-style fork test sketch:

```solidity
// FeeOnTransferMock: takes 1% fee on every transfer/transferFrom
vm.prank(governor);
pool.addDepositToken(fotDepositToken); // FOT underlying registered

// 1. Alice deposits 100 FOT; treasury receives 99, msd supply = 99
fot.approve(address(depositToken), 100e18);
depositToken.deposit(100e18, alice);
assertEq(depositToken.totalSupply(), 99e18);          // delta-accounted
assertEq(fot.balanceOf(address(treasury)), 99e18);

// 2. Bob deposits 100 FOT; treasury = 198, supply = 198
// ...same as above...

// 3. Alice withdraws 99: treasury sends 99, alice receives 98.01
depositToken.withdraw(99e18, alice);
assertEq(depositToken.totalSupply(), 99e18);
// treasury balance = 198 - 99 = 99, supply = 99 -> still consistent

// Repeat deposits+withdraws; each withdraw leaks `fee` units that were
// burned as supply but left the treasury as value received by user.
// After N withdrawals: fot.balanceOf(treasury) < totalSupply().
// Eventually bob's withdraw reverts:
vm.expectRevert(); // ERC20: transfer amount exceeds balance
depositToken.withdraw(depositToken.balanceOf(bob), bob);
```

The reverts at `Treasury.sol:71` (`safeTransfer`) demonstrate the permanent freeze of the residual depositors' funds.