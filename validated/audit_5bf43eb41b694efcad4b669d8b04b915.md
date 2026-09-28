### Title
Dust-deposit griefing fills victim's `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, blocking new collateral deposits and new debt positions - ([File: contracts/Pool.sol])

### Summary
`DepositToken.deposit(amount_, onBehalfOf_)` mints `msdTOKEN` to an arbitrary `onBehalfOf_` address. On a first-time balance, `_mint`/`_transfer` calls `Pool.addToDepositTokensOfAccount(account_)`, which enforces `MAX_TOKENS_PER_USER = 30` over the *combined* `debtTokensOfAccount` + `depositTokensOfAccount` sets. An unprivileged attacker can permissionlessly push dust deposits of every registered `DepositToken` (and/or `transfer` dust `msdTOKEN`) to a victim, occupying the victim's per-account slots. Once the cap is hit, any subsequent action that would add a *new* token to the victim's account reverts with `UserReachedMaxTokens`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
- `DepositToken.deposit` accepts any `onBehalfOf_` beneficiary and calls `_mint(onBehalfOf_, _deposited)`. `_mint` adds the token to `pool.depositTokensOfAccount` when the recipient's balance goes `0 -> >0` (`DepositToken.sol:486-488`). The same happens on `_transfer` to a first-time recipient (`DepositToken.sol:518-520`).
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:143-148, 204-220`).
- `DebtToken._mint` similarly calls `pool.addToDebtTokensOfAccount(account_)` on a first borrow (`DebtToken.sol:598-600`), so a maxed-out account cannot open a debt position in a synthetic it does not already hold — `issue()` reverts.
- There is no opt-out or minimum-amount check: a deposit of 1 wei of underlying (`_deposited > 0`) occupies one slot forever until the victim's balance returns to zero.

### Impact Explanation
Temporary freezing of funds / liveness DoS:
- The victim cannot deposit into any `DepositToken` not already in their set, and cannot receive first-time `msdTOKEN` transfers — every such tx reverts (`UserReachedMaxTokens`).
- The victim cannot `issue` a synthetic whose `DebtToken` is not already in their set (`DebtToken._mint -> addToDebtTokensOfAccount` reverts).
- Critically, a victim near liquidation cannot deposit a *new* collateral type to restore health; if their existing collateral is insufficient/locked, they are forced into liquidation, converting griefing into realized loss. An attacker can combine this with same-transaction price moves to liquidate a position that the victim could otherwise have saved.
- Recovery is asymmetric: the victim must transfer out dust balances one token at a time (each `transfer` removes the token only when the balance hits zero, `DepositToken.sol:523-525`), and the attacker can re-fill freed slots by re-depositing dust, so the victim must race the attacker while their position decays.

### Likelihood Explanation
- Fully permissionless: `deposit` only requires `whenNotPaused`, `onlyIfDepositTokenExists`, and a nonzero amount (`DepositToken.sol:211-216`); `transfer` only requires unlocked balance (`DepositToken.sol:348-354`). No role, approval, or oracle needed.
- Cost is bounded by dust deposits/transfers across the registered deposit tokens in the pool. Feasibility requires the pool to list enough distinct `DepositToken`s (plus any debt tokens the victim already holds counting toward the same 30-slot cap) to reach the cap; the victim's own existing tokens count toward the cap, lowering the number of tokens the attacker must push.
- Not blocked by `nonReentrant`, `SynthContext`, pause flags, or supply caps (dust amounts are far below `maxTotalSupply`).

### Recommendation
- Only add a token to `depositTokensOfAccount` when the account initiates the deposit itself (i.e., restrict `onBehalfOf_` additions, or let beneficiaries claim rather than being force-minted), and/or
- Require a minimum first-deposit amount to open a slot, and/or
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` failure non-fatal for *received* transfers by tracking opted-in positions separately from forced dust balances.

### Proof of Concept
Hardhat sketch (reproducible on a mainnet fork with the deployed `Pool`, `DepositToken`s, and `DebtToken`s):

```ts
// Assume pool has N registered deposit tokens d1..dN and victim has some open position.
const victim = alice.address

// 1) Attacker fills victim's account slots with dust deposits.
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, 1)
  // first-time balance for victim -> adds dt to depositTokensOfAccount[victim]
  await dt.connect(attacker).deposit(1, victim) // dust onBehalfOf victim
}

// (equivalently: attacker deposits to self, then `transfer(victim, 1)` dust)

// 2) Victim now has debtTokens + depositTokens == MAX_TOKENS_PER_USER (30).
expect(
  (await pool.getDepositTokensOfAccount(victim)).length +
  (await pool.getDebtTokensOfAccount(victim)).length
).to.eq(30)

// 3) Victim cannot deposit into a new collateral type.
await expect(
  newDepositToken.connect(alice).deposit(parseEther('1'), victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4) Victim cannot issue a synthetic whose debt token isn't already tracked.
await expect(
  newDebtToken.connect(alice).issue(parseEther('1'), victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 5) While capped, attacker manipulates collateral price down (same-tx AMM/oracle path
//    allowed by scope); victim cannot add new collateral to restore health and is
//    liquidated via pool.liquidate(...).
```

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
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

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```
