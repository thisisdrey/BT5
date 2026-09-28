### Title
Attacker can grief any account by filling its per-account token lists to `MAX_TOKENS_PER_USER` with dust transfers, blocking deposits/issues and forcing liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
CVE-2020-35521 is a DoS caused by an unexpected allocation failure turning into an abort. The Metronome analog is the per-account token-list accounting in `Pool`: `depositTokensOfAccount`/`debtTokensOfAccount` are capped at `MAX_TOKENS_PER_USER = 30`, and `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens()` once the combined count reaches 30 [1](#0-0) [2](#0-1) . Because `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to non-zero [3](#0-2) , an unprivileged attacker can force-fill a victim's list by dust-transferring every listed deposit token to them, after which any new-token deposit, inbound transfer, or debt issuance to that account aborts — mirroring the "allocation failure → abort" bug class.

### Finding Description
- `Pool.MAX_TOKENS_PER_USER = 30` bounds `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account)` [1](#0-0) [2](#0-1) .
- `addToDepositTokensOfAccount` is externally callable only by registered deposit tokens, but it is invoked inside `DepositToken._transfer`/`_mint`/`seize` on behalf of whatever recipient the caller chooses [4](#0-3) [3](#0-2) [5](#0-4) .
- Attack: the attacker deposits a minimal amount of underlying into each of the pool's deposit tokens (up to 30, since `addDepositToken` is also capped at `MAX_TOKENS_PER_USER` [6](#0-5) ), then calls `DepositToken.transfer(victim, dust)` for each token. Each transfer inserts that token into `depositTokensOfAccount[victim]`. Once the victim's combined deposit+debt list reaches 30:
  - `deposit(...)` / `depositOnBehalf` for any collateral the victim does not already hold reverts in `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens()` [7](#0-6) .
  - `DebtToken` issuance of a new synthetic reverts in `addToDebtTokensOfAccount` [8](#0-7) .
  - Any third-party `transfer`/`transferFrom` of a deposit token the victim doesn't hold reverts.
- Victim mitigation is to fully empty one dust position to free a slot, but the attacker can front-run/refill the freed slot with a fresh dust transfer (adds are O(1) and cheap), so the DoS is sustainable at low cost.

### Impact Explanation
- Temporary freezing of funds / forced liquidation: a borrower close to the liquidation threshold who needs to add a *new* collateral type to restore health is blocked; their position can be liquidated while deposits revert, incurring the liquidation fee loss. This is a concrete liveness break, not just gas inefficiency.
- Blocking issuance of new debt token types and inbound transfers is also a denial of service on the account.
- The abort is deterministic (a `require`-style revert at the cap), structurally identical to the reference CVE where an allocation failure deterministically aborts processing of user-supplied input.

### Likelihood Explanation
- Attacker is an unprivileged EOA; cost is the dust value of up to 30 deposit-token positions plus gas. Deposits and transfers are public entry points; no privileged role, oracle manipulation, or governance action is needed.
- Limited to one victim at a time and yields no direct profit — it's griefing — so real-world likelihood is moderate; economic incentive exists when forcing a liquidation yields a bonus or when combined with keep-the-collateral strategies.
- `ReentrancyGuard`, `SynthContext`, and pause flags do not prevent it: `transfer` is unpaused functionality and the cap check is the mechanism itself.

### Recommendation
- Only revert on cap overflow when the caller initiated the addition to its own list (e.g., skip `addToDepositTokensOfAccount` for recipients who didn't opt in), or
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` non-reverting at capacity (emit an event and skip), and/or raise the cap, and/or let users self-clear dust entries. Track deposits for health only for tokens the user opted into is another alternative.

### Proof of Concept
Hardhat sketch:

```ts
// victim has a debt position; attacker griefts their token list
for (const dt of allDepositTokens) {          // up to 30 pool deposit tokens
  await underlying.approve(dt.address, DUST);
  await dt.deposit(DUST, attacker.address);    // attacker mints dust msdTOKEN
  await dt.transfer(victim.address, DUST);     // inserts dt into victim's list
}
// victim's combined list is now at MAX_TOKENS_PER_USER
await expect(
  newDepositToken.deposit(amount, victim.address) // depositOnBehalf path
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

await expect(
  otherHolder.transfer(victim.address, amount)    // inbound transfer of new token
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim near liquidation cannot add the new collateral -> liquidate succeeds
await pool.liquidate(victim.address, repayAmt, seizedToken.address, synth.address);
```

Fork test: impersonate an EOA, loop over `pool.getDepositTokens()`, deposit 1 unit and `transfer` it to the target; assert subsequent new-token deposits revert with `UserReachedMaxTokens` while `depositOf`/`debtPositionOf` still compute normally [9](#0-8) .

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

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
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

**File:** contracts/Pool.sol (L274-288)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
    }
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L486-488)
```text
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

**File:** test/Pool.test.ts (L1236-1256)
```typescript
      it('should revert if reach max', async function () {
        // given
        const current = await pool.getDepositTokens()
        // eslint-disable-next-line new-cap
        const max = await pool.MAX_TOKENS_PER_USER()
        const n = max.sub(current.length).toNumber()

        for (let i = 0; i < n; ++i) {
          const deposit = await smock.fake('DepositToken')
          deposit.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await pool.addDepositToken(deposit.address)
        }

        // when
        const deposit = await smock.fake('DepositToken')
        deposit.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
        const tx = pool.addDepositToken(deposit.address)

        // then
        await expect(tx).revertedWithCustomError(pool, 'ReachedMaxDepositTokens')
      })
```
