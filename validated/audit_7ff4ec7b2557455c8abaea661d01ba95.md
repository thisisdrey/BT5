### Title
Withdrawal assumes full token transfer — fee-on-transfer collateral causes permanent loss of user funds ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The GroupBuy bug class is an accounting asymmetry: the protocol records/reimburses at an assumed (higher) price while the actual amount moved is lower, leaving the difference as unrecoverable user loss. Metronome exhibits the same asymmetry in the collateral withdrawal path: `deposit()` correctly measures the *actual* tokens received by the Treasury via a balance delta, but `_withdraw()` assumes `Treasury.pull()` delivers the full `_withdrawn` amount to the user. For a collateral token that charges a transfer fee, the user's msdTOKEN shares are burned for the full amount while they receive less underlying — the difference is a permanent, unrecoverable loss, identical in structure to `contribution -= quantity * reservePrice` when the NFT was bought cheaper.

### Finding Description
`DepositToken.deposit` pulls collateral to the Treasury and re-bases `amount_` on the actual received delta, so fee-on-transfer tokens are accounted correctly on entry [1](#0-0) .

`_withdraw` computes `_withdrawn` net of the protocol `withdrawFee`, burns `_withdrawn` shares, and instructs the Treasury to send exactly `_withdrawn` underlying [2](#0-1) . `Treasury.pull` performs a plain `safeTransfer(to_, amount_)` with no post-transfer balance check [3](#0-2) . If the underlying ERC20 deducts a transfer fee, the recipient receives `_withdrawn - tokenFee` while the full `_withdrawn` share amount was burned — the user never gets the difference back and it is not credited to anyone.

The codebase's own test confirms the asymmetric treatment: `test/Pool.test.ts` charges a 10% fee on MET and shows `withdraw` delivering only the fee-reduced amount while the full share balance is consumed [4](#0-3) .

### Impact Explanation
Every withdrawal of a fee-on-transfer collateral loses `tokenFee * amount` of user funds. Withdrawals via `SmartFarmingManager.flashRepay` (`flashWithdraw`) suffer the same haircut, and the lost amount is not donated back to remaining depositors — it is stranded. Direct loss of user funds proportional to the token's transfer fee, matching the "reimbursed at assumed price, paid at actual price" invariant break of the reference bug.

### Likelihood Explanation
The protocol explicitly supports fee-charging collateral (balance-delta accounting on deposit, dedicated tests). Any listed collateral that enables or raises a transfer fee (e.g., tokens with dynamic fee toggles) triggers the loss on the next withdrawal; no attacker action is required, and an attacker cannot front-run to avoid it — the loss is inherent to the accounting assumption.

### Recommendation
Mirror the deposit-side fix on the withdrawal side: measure the actual amount delivered, e.g. have `Treasury.pull` (or `_withdraw`) compare `underlying.balanceOf(to_)` before/after the transfer and burn only the shares corresponding to the amount actually received, or have `pull` return the delta so `_withdraw` can reimburse the user for the token-level fee.

### Proof of Concept
1. List a collateral whose ERC20 charges fee `f` on transfers (the test suite already models this via `met.updateFee`).
2. User calls `deposit(100)` → Treasury receives `100·(1−f)`, user is minted `~100·(1−f)` msdTOKEN (correct).
3. User calls `withdraw(all)` → `_withdraw` burns the full `_withdrawn` shares and `Treasury.pull` sends `_withdrawn`, but only `_withdrawn·(1−f)` arrives. The user loses `f·_withdrawn` with no reimbursement — the analog of losing `contribution` over the unspent reserve in GroupBuy.

Note: this analog holds only for fee-on-transfer underlying tokens; I could not verify from the index whether any deployed collateral currently charges transfer fees, so deployed-configuration confirmation would require a fork test.

### Citations

**File:** contracts/DepositToken.sol (L225-227)
```text
        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
```

**File:** contracts/DepositToken.sol (L545-551)
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

**File:** test/Pool.test.ts (L263-278)
```typescript
    it('should withdraw when collateral charges transfer fee', async function () {
      // given
      const fee = parseEther('0.1') // 10%
      await met.updateFee(fee)
      const metBalanceBefore = await met.balanceOf(alice.address)
      const amountToWithdraw = await msdMET.balanceOf(alice.address)

      // when
      const amountAfterFee = amountToWithdraw.sub(amountToWithdraw.mul(fee).div(parseEther('1')))
      const tx = msdMET.connect(alice).withdraw(amountToWithdraw, alice.address)
      await expect(tx)
        .emit(msdMET, 'CollateralWithdrawn')
        .withArgs(alice.address, alice.address, amountToWithdraw, amountToWithdraw, 0)

      // then
      expect(await met.balanceOf(alice.address)).eq(metBalanceBefore.add(amountAfterFee))
```
