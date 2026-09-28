### Title
`approve` allows a spender to consume both the old and replacement allowance through front-running - ([File: contracts/DepositToken.sol])

### Summary
`DepositToken.approve` unconditionally replaces the existing allowance. If an owner changes a spender’s nonzero allowance to another nonzero value, the spender can front-run the replacement transaction, spend the old allowance, and then spend the newly approved allowance. The same unrestricted allowance replacement exists in `SyntheticToken.approve`.

### Finding Description
`DepositToken.approve` directly calls `_approve(_msgSender(), spender_, amount_)` without requiring either the old or new allowance to be zero. A subsequent `transferFrom` deducts from the currently stored allowance, so the spender can execute `transferFrom` between the original approval and the replacement approval.

A concrete sequence is:

1. Alice calls `approve(Bob, N)`.
2. Alice later submits `approve(Bob, M)` to change the allowance.
3. Bob observes the pending transaction and calls `transferFrom(Alice, Bob, N)` first.
4. Alice’s transaction then resets Bob’s allowance to `M`.
5. Bob calls `transferFrom(Alice, Bob, M)`.

Bob therefore receives `N + M` deposit tokens even though Alice intended Bob’s total replacement allowance to be `M`.

For `DepositToken`, the transferred amount is limited by `unlockedBalanceOf(Alice)`, so this affects Alice’s transferable collateral position. For `SyntheticToken`, ordinary balances are transferable without that collateral-health restriction.

### Impact Explanation
An unprivileged spender can take up to `N` tokens beyond the allowance the owner intended to leave after the approval update. If the victim has enough unlocked deposit-token balance, the spender can directly receive `N + M` deposit tokens. The stolen deposit-token balance represents a claim on deposited collateral, while stolen synthetic tokens are directly transferable protocol assets.

This is a loss of user funds caused by transaction ordering; no privileged role, oracle manipulation, malformed contract argument, or protocol invariant bypass is required.

### Likelihood Explanation
Likelihood depends on an owner using `approve` to replace a nonzero allowance while the current spender can observe the mempool. Public-mempool transactions and automated spending contracts make this feasible. The owner can avoid the race by using `decreaseAllowance`/`increaseAllowance`, but the exposed `approve` function still permits the vulnerable sequence.

### Recommendation
Restrict `approve` so it can only set an initial allowance or reset an allowance to zero:

```solidity
function approve(address spender_, uint256 amount_) external override returns (bool) {
    address _msgSender = _msgSender();
    if (amount_ != 0 && allowance[_msgSender][spender_] != 0) {
        revert ApproveFromNonZeroToNonZeroAllowance();
    }

    _approve(_msgSender, spender_, amount_);
    return true;
}
```

Users should then modify nonzero allowances through the existing `increaseAllowance` and `decreaseAllowance` functions. Apply the same restriction to `SyntheticToken.approve`.

### Proof of Concept
The following Hardhat-style test demonstrates the transaction ordering using the existing `DepositToken` fixture:

```ts
it('lets spender consume old and replacement allowances', async function () {
  const n = parseEther('100')
  const m = parseEther('40')

  await metDepositToken.connect(alice).approve(bob.address, n)

  // Bob front-runs Alice's pending approve(bob, m) transaction.
  await metDepositToken.connect(bob).transferFrom(alice.address, bob.address, n)

  // Alice's replacement approval succeeds and restores a nonzero allowance.
  await metDepositToken.connect(alice).approve(bob.address, m)

  // Bob can spend the replacement allowance too.
  await metDepositToken.connect(bob).transferFrom(alice.address, bob.address, m)

  expect(await metDepositToken.balanceOf(bob.address)).to.eq(n.add(m))
  expect(await metDepositToken.allowance(alice.address, bob.address)).to.eq(0)
})
```

The test assumes Alice has at least `N + M` unlocked deposit-token balance. For `SyntheticToken`, the same call ordering succeeds without an unlocked-balance precondition beyond Alice’s token balance.