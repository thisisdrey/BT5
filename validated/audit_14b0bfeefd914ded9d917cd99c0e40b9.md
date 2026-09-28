### Title
Attacker can grief an account by filling its per-account token list to `MAX_TOKENS_PER_USER` with dust deposits/transfers, DoS-ing new collateral deposits, new synth issuance, and leverage - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across the union of `debtTokensOfAccount` and `depositTokensOfAccount` [1](#0-0) [2](#0-1) . Entries are added automatically whenever an account's balance in a deposit or debt token goes from 0 to positive — inside `DepositToken._mint`/`_transfer` and `DebtToken` issuance — via the pool callbacks `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`, which revert with `UserReachedMaxTokens` once the cap is hit [3](#0-2) [4](#0-3) [5](#0-4) . An unprivileged attacker can therefore push a victim's list to 30 entries (deposit dust `onBehalfOf` the victim or transfer dust of every whitelisted deposit token, plus `issue`/`mint` dust debt to a victim-benefiting flow), permanently reverting any subsequent operation that would add a *new* token to that account's list.

### Finding Description
Bug class mapped from the CVE hint: remote-triggered denial of service via attacker-injectable state in an otherwise benign code path — here, an unsolicited 0→positive balance update on someone else's account.

Attack path (all public, unprivileged):

1. Attacker deposits dust amounts of each whitelisted collateral through `DepositToken.deposit(amount_, victim)` — `deposit` mints to `onBehalfOf_` [6](#0-5) , and `_mint` calls `pool.addToDepositTokensOfAccount(victim)` when the balance was zero [5](#0-4) . Alternatively, `transfer`/`transferFrom` to the victim do the same via `_transfer` [7](#0-6) [4](#0-3) .
2. `addToDepositTokensOfAccount` is guarded only by `onlyIfAdditionWillNotReachMaxTokens(victim)`, which counts `debtTokensOfAccount + depositTokensOfAccount` [2](#0-1) [8](#0-7) . The pool itself caps whitelisted deposit tokens at the same 30 [9](#0-8) , so a fully populated list is reachable whenever the pool has enough listed assets.
3. Once the victim's combined list length reaches 30, the following permanently revert for the victim:
   - `DepositToken.deposit` of any *new* collateral type to their account (`_mint` → add → `UserReachedMaxTokens`).
   - Any incoming `transfer`/`transferFrom`/`seize` of a deposit token they don't already hold — including liquidation proceeds (a liquidator with a saturated list also cannot be paid a new collateral type via `DepositToken.seize`, which routes through `_transfer` [10](#0-9) ).
   - Issuing/minting any *new* synthetic (`DebtToken` issuance calls `pool.addToDebtTokensOfAccount`, which has the same modifier [11](#0-10) ), which also breaks `SmartFarmingManager.leverage`-style flows that mint new debt on the account.

Nothing stops this on the deployed configuration: the modifier is the only check, `SynthContext._msgSender`/`onlyPool`/reentrancy guards/pause flags are irrelevant to the counter being griefed, and the victim's own pre-existing balances still work — only *new* list entries fail.

### Impact Explanation
Temporary freezing of funds / liveness break: the victim cannot onboard new collateral or borrow new synthetics, cannot be liquidated into a collateral token they don't already hold (seize reverts), and cannot receive deposits made on their behalf, until they manually unwind the dust positions (each removal requires zeroing that token's balance via `withdraw`/`transfer`, which costs them gas and may be impossible for locked dust while they carry debt — `_revertIfLocked`/`unlockedBalanceOf` can lock even small balances when the position is at its collateral limit [12](#0-11) [13](#0-12) ). For leveraged users near their limit, the dust may be effectively locked, making the freeze long-lived and blocking deleveraging that requires depositing a different collateral.

### Likelihood Explanation
Any EOA can execute this with only dust value (1 wei per token) and gas; the deposit path requires no token balance on the victim and no cooperation. Feasibility depends on the deployed pool listing enough deposit+debt tokens to reach 30 combined entries — deployments (mainnet, base, optimism, swell, hemi, bsc) all carry this code [14](#0-13) . No privileged role, oracle manipulation, or trusted-remote assumption is needed. The main caveat: if a deployed pool lists far fewer than 30 tokens total, the cap may not be reachable; the attack requires `listed deposit tokens + listed debt tokens ≥ 30` or the victim already holding several tokens.

### Recommendation
- Only count tokens toward `MAX_TOKENS_PER_USER` when the balance increase is initiated/benefited by the account owner, or move the cap check off the revert path: e.g., allow the add to silently no-op (or track the token in an "overflow" list that is excluded from `debtPositionOf` iteration only for accounting the user opted into).
- Cheapest fix: in `addTo{Deposit,Debt}TokensOfAccount`, check whether `account_` opted in (e.g., `deposit` should add to `onBehalfOf_` only if `onBehalfOf_ == _msgSender()` or an explicit `allowList` mapping), and/or let `seize` bypass the cap so liquidations are never blocked.
- Alternatively revert only on *issuer-initiated* additions: keep the cap for `DebtToken` issuance but make `DepositToken` additions unbounded-or-opt-in, since inbound transfers are not consented to.

### Proof of Concept
Hardhat sketch against the real `Pool`/`DepositToken` deployment:

```ts
// Setup: pool with N whitelisted DepositTokens and M DebtTokens, N + M >= 30
// victim has some position already
for (const dt of depositTokens) {
  // attacker deposits 1 wei of underlying to the victim
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim.address); // _mint -> addToDepositTokensOfAccount(victim)
}
// ... same for remaining debt/deposit tokens until list length == 30
expect(await pool.getDepositTokensOfAccount(victim.address))
  .length.plus(await pool.getDebtTokensOfAccount(victim.address)).to.eq(30);

// 1) Victim cannot deposit a collateral type they don't hold
await expect(
  newDepositToken.connect(victim).deposit(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) Victim cannot issue a synth whose debt token they don't hold
await expect(
  newDebtToken.connect(victim).issue(parseEther('1'))
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) Liquidation seizing a not-yet-held collateral token to the victim/liquidator reverts
await expect(
  pool.connect(liquidator).liquidate(victim.address, synth.address, amount, newDepositToken.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

The existing unit test already demonstrates the revert at the cap (`should revert when reach max tokens`) [15](#0-14) ; the PoC only needs real `DepositToken.deposit`/`transfer` calls on behalf of a non-consenting victim instead of mocked token callers.

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

**File:** contracts/Pool.sol (L703-705)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
```

**File:** contracts/DepositToken.sol (L230-236)
```text
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** deployments/base/Pool.json (L1901-1905)
```json
      "addToDebtTokensOfAccount(address)": {
        "notice": "Add a debt token to the per-account list"
      },
      "addToDepositTokensOfAccount(address)": {
        "notice": "Add a deposit token to the per-account list"
```

**File:** test/Pool.test.ts (L1386-1416)
```typescript
      it('should revert when reach max tokens', async function () {
        // given
        const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()
        const accountAddress = ethers.utils.hexlify(ethers.utils.randomBytes(20))

        for (let i = 0; i < max / 2; ++i) {
          const deposit = await smock.fake('DepositToken')
          deposit.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(deposit.address, '0x01')
          await setBalance(deposit.address, parseEther('1'))

          await pool.addDepositToken(deposit.address)
          await pool.connect(deposit.wallet).addToDepositTokensOfAccount(accountAddress)
        }

        for (let i = 0; i < max / 2; ++i) {
          const debt = await smock.fake('DebtToken')
          debt.syntheticToken.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(debt.address, '0x01')
          await setBalance(debt.address, parseEther('1'))

          await pool.addDebtToken(debt.address)
          await pool.connect(debt.wallet).addToDebtTokensOfAccount(accountAddress)
        }

        // then
        const tx = pool.connect(msdTOKEN.wallet).addToDepositTokensOfAccount(accountAddress)

        // when
        await expect(tx).revertedWithCustomError(pool, 'UserReachedMaxTokens')
      })
```
