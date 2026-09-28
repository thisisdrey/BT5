### Title
Attacker fills victim's `depositTokensOfAccount`/`debtTokensOfAccount` to `MAX_TOKENS_PER_USER` via dust deposits on behalf of the victim, DoSing deposits, borrows, and collateral top-ups - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome tracks each account's held deposit/debt tokens in per-account `MappedEnumerableSet`s and caps the combined length at `MAX_TOKENS_PER_USER = 30`. `DepositToken.deposit(amount_, onBehalfOf_)` is permissionless and mints msdTOKEN to an arbitrary `onBehalfOf_`, which calls `pool.addToDepositTokensOfAccount(onBehalfOf_)` whenever the recipient's balance was zero. There is no opt-in. An attacker can deposit dust amounts of every listed deposit token directly into a victim's account until the cap is reached. From then on, every path that adds a new token to the victim's lists reverts with `UserReachedMaxTokens`: depositing a new collateral type, receiving msdTOKEN transfers, issuing/minting a new synthetic debt, and liquidation `seize` into a fresh token. This is the direct analog of the `MAX_DELEGATES` delegation griefing in the reference report: an unprivileged attacker forces entries onto a victim's bounded list to DoS their core operations.

### Finding Description
- `Pool.sol:79` sets `uint256 public constant MAX_TOKENS_PER_USER = 30`.
- `Pool.sol:143-148` (`onlyIfAdditionWillNotReachMaxTokens`) reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30`. [1](#0-0) 
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` (Pool.sol:204-220) apply that modifier and are callable only by registered deposit/debt tokens. [2](#0-1) 
- `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` (DepositToken.sol:211-237) pulls `amount_` of underlying from the caller and mints to `onBehalfOf_`. `_mint`/`_transfer` add the token to the recipient's list when the prior balance was 0 (DepositToken.sol:486-488, 518-519). [3](#0-2) 
- `DebtToken` mint/issue paths call `pool.addToDebtTokensOfAccount(account)` on first balance, sharing the same 30-slot budget.

Attack sequence:
1. Attacker acquires dust amounts of each underlying listed in the target pool (or flash-borrows them and redeposits serially).
2. For each registered `DepositToken`, attacker calls `depositToken.deposit(dust, victim)`. Each call adds one entry to `depositTokensOfAccount[victim]`.
3. Once `length == 30`, the victim's subsequent `deposit` of any collateral they don't already hold, any `transfer`/`transferFrom` of a new msdTOKEN to them, and any `DebtToken.issue`/`mint` of a new synthetic all revert with `UserReachedMaxTokens`.
4. Cleanup requires the victim to transfer out each dust position individually (balance must hit exactly 0 for `removeFromDepositTokensOfAccount`), and the attacker can re-grief by depositing a new dust token in the same or next block — cheap, since dust deposit cost is minimal on L2 deployments (Optimism, Base, Hemi, Swell deployments exist).

No privileged role, oracle manipulation, or governance action is required; `deposit` is `whenNotPaused nonReentrant onlyIfDepositTokenExists` and none of these stop the griefing.

### Impact Explanation
Temporary freezing of funds / liveness failure and forced-liquidation griefing:
- A victim with an unhealthy position cannot deposit a *new* collateral type to restore health — the mint reverts — so they can be liquidated where they otherwise would have topped up collateral. Their only escape is paying down debt or spending gas on up to 30 outbound dust transfers first.
- The victim cannot open debt positions in new synthetics or receive new msdTOKENs.
- Liquidators calling `Pool.liquidate` → `DepositToken.seize` into a liquidator account already at 30 tokens also revert, which can impede liquidations.
Because entries are removable, the freeze is temporary rather than permanent, but it is repeatable at negligible attacker cost (each dust deposit is one cheap ERC20 transfer on the underlying).

### Likelihood Explanation
High feasibility: attacker only needs dust of each listed underlying and standard `deposit` calls; no capital lockup is required since dust amounts are near-zero value (and can even be flash-sourced and re-used across sequential deposits of the same token to different victims). Constraint: the pool must list enough deposit tokens to reach 30 combined entries, or the attacker partially fills the list (each forced entry still consumes a slot the victim may need). Profit motive is weak (pure griefing / enabling liquidation), matching the reference bug class.

### Recommendation
- Add an opt-in for receiving new deposit tokens (e.g., only add to `depositTokensOfAccount` when the recipient called `deposit` itself, or a per-account whitelist/`toggleAcceptDepositToken`), or make `deposit(..., onBehalfOf_)` not register the token for `onBehalfOf_` unless they already hold it or opted in.
- Alternatively, allow anyone to remove an entry from their own lists (e.g., a `pool.removeFromDepositTokensOfAccount` path callable by the account for dust balances), and/or raise `MAX_TOKENS_PER_USER` / decouple debt and deposit limits.
- Consider not counting positions below a minimum USD threshold toward the cap.

### Proof of Concept
Hardhat-style PoC (against deployed pool with `N` deposit tokens; assumes attacker holds dust of each underlying):

```ts
// victim has an open position; attacker griefs
const victim = bob.address
const depositTokens: DepositToken[] = await getAllDepositTokens(pool) // pool.depositTokens()

for (const dt of depositTokens.slice(0, 30)) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  const dust = ethers.BigNumber.from('1') // 1 wei of underlying
  await underlying.connect(attacker).approve(dt.address, dust)
  await dt.connect(attacker).deposit(dust, victim) // adds to depositTokensOfAccount[victim]
}

// victim's list is now full
expect((await pool.getDepositTokensOfAccount(victim)).length +
       (await pool.getDebtTokensOfAccount(victim)).length).to.eq(30)

// victim cannot deposit a new collateral type
const newDt = depositTokens[30] // a deposit token victim doesn't hold
const newUnderlying = await ethers.getContractAt('IERC20', await newDt.underlying())
await newUnderlying.connect(bob).approve(newDt.address, parseEther('1'))
await expect(newDt.connect(bob).deposit(parseEther('1'), victim))
  .to.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim cannot mint a new synthetic debt token either
await expect(newDebtToken.connect(bob).issue(parseEther('1'), victim))
  .to.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// attacker re-griefs after victim clears one slot
await depositTokens[0].connect(bob).transfer(attacker.address, 1) // clears slot
await depositTokens[30].connect(attacker).deposit(1, victim)      // refills it
```

For a fork PoC, deploy/harvest dust via an AMM swap of each underlying to the attacker, then loop the deposits; assert `UserReachedMaxTokens` on the victim's recovery-deposit while its position is liquidatable.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L486-489)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```
