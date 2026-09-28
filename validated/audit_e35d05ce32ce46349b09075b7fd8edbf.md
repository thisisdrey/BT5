### Title
Dust-transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, permanently blocking them from receiving deposit tokens, topping up collateral, or issuing debt - (File: contracts/Pool.sol)

### Summary
The AutonatV2 bug class is "shared state is mutated to an unusable/nil form while concurrent readers keep dereferencing it, turning every subsequent read into a panic." The Metronome analog is the per-account enumerable sets `depositTokensOfAccount`/`debtTokensOfAccount` in `Pool`: any registered `DepositToken` may append an entry for any account when that account's balance goes `0 → >0`, and `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the combined list length hits `MAX_TOKENS_PER_USER`. An unprivileged attacker can permanently poison a victim's list by dust-transferring `1 wei` of each registered `DepositToken` to them — after that, every code path that tries to add a new token to the victim's list (deposits on their behalf, incoming `DepositToken` transfers, `issue`/`flashIssue` of a new `DebtToken`) reverts.

### Finding Description
`DepositToken._transfer` appends the token to the *recipient's* list whenever the recipient's prior balance is zero: [1](#0-0) 

`Pool.addToDepositTokensOfAccount` is the reader that panics once the list is full: [2](#0-1) [3](#0-2) 

Because the dust sits in the victim's `balanceOf`, the entries cannot be removed except by the victim transferring/burning them — and `DepositToken.transfer` is gated by `_revertIfLocked`/`unlockedBalanceOf`, so if the victim's collateral is fully locked against outstanding debt they cannot dispose of the dust at all. The victim's `depositTokensOfAccount` set is then permanently saturated: exactly the "closed/nil'd structure still being read" failure mode — every subsequent `add` path panics (`UserReachedMaxTokens`) rather than degrading gracefully.

There is no `onlyGovernor`, guardian, or trusted-role involvement anywhere in the chain: `DepositToken.transfer` is a public ERC20 entry point, and `addToDepositTokensOfAccount` accepts any registered `DepositToken` as `_msgSender`.

### Impact Explanation
- **Temporary/permanent freezing of funds:** once saturated, `deposit(underlying_, victim)` (including `NativeTokenGateway`/`VesperGateway` deposit paths that mint `msd*` to the victim) and `pool.issue`/`flashIssue` of any *new* synthetic debt for the victim revert with `UserReachedMaxTokens`. If the victim's existing collateral is locked by debt, the dust cannot be removed, so the freeze is permanent until the debt is repaid — which the victim may be unable to do if their only path was new deposits.
- **Forced liquidation / indirect loss:** a victim approaching the liquidation threshold cannot top up with a collateral type not already in their list; a keeper's protective top-up on their behalf also reverts. The position gets liquidated at a loss the victim could have avoided.

### Likelihood Explanation
- Cost is bounded: the attacker needs only dust (`1 wei`) of each registered `DepositToken`, obtainable by depositing dust of each underlying themselves.
- Feasibility depends on `MAX_TOKENS_PER_USER` relative to the number of registered `DepositToken`s. The modifier counts `debtTokensOfAccount + depositTokensOfAccount` combined, so existing entries reduce the attacker's work. Caveat: if `MAX_TOKENS_PER_USER` exceeds the number of registered deposit tokens plus the victim's existing entries, saturation via dust alone is unreachable — this constant and the live registered-token count were not fully verifiable from the index and must be confirmed against the deployment.
- No timing assumption is needed; unlike the Go race, the Ethereum analog is deterministic within a single transaction.

### Recommendation
- Make `DepositToken._transfer`/`_mint` skip `pool.addToDepositTokensOfAccount` for unsolicited dust (e.g., only add on `deposit` paths) or treat the `add` failure as non-fatal in `transfer`, so a failed list insertion doesn't revert but also can't be weaponized — symmetrically, let `addToDepositTokensOfAccount` no-op (return silently) instead of reverting when at cap, moving the cap check to `deposit`/`issue` entry points where it belongs.
- Alternatively allow anyone to remove dust below a threshold, or exempt `transfer` received balances from the per-account list.

### Proof of Concept
Hardhat/fork sketch (assumes N registered deposit tokens reach the cap):

```ts
it('dust-griefs victim token list', async () => {
  // attacker deposits dust of every underlying to obtain msd tokens
  for (const dt of registeredDepositTokens) {
    const underlying = await ethers.getContractAt('ERC20', await dt.underlying())
    await underlying.connect(attacker).approve(dt.address, 1)
    await dt.connect(attacker).deposit(1, attacker.address)
    // 1 wei of msdTOKEN to victim => appends to victim's list
    await dt.connect(attacker).transfer(victim.address, 1)
  }
  // victim's combined list is now at MAX_TOKENS_PER_USER
  // any new deposit to victim reverts
  await expect(
    someDepositToken.connect(alice).deposit(parseEther('1'), victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
  // victim cannot transfer the dust away while collateral is locked
  await expect(
    msdTOKEN.connect(victim).transfer(attacker.address, 1)
  ).to.be.revertedWithCustomError(msdTOKEN, 'NotEnoughFreeBalance')
})
```

Note: reproduce against the deployed pool to confirm `MAX_TOKENS_PER_USER` is reachable with the registered token set; if it is not, this finding reduces to a non-issue and should be discarded.

### Citations

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```
