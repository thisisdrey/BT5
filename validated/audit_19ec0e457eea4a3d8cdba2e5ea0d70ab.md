### Title
Attacker can fill a victim's per-account token list with dust deposit-token transfers, DoSing deposits into new collaterals - (File: contracts/Pool.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` on the combined length of `debtTokensOfAccount` and `depositTokensOfAccount`. `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever a recipient's balance moves from zero to non-zero, and `addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the victim's list is full. Because `DepositToken` is a permissionless ERC20, an unprivileged attacker can push dust amounts of every registered deposit token to a victim, filling their list and making any subsequent first-time deposit into a new collateral revert — the same MAX_DELEGATES dust-griefing class as the Alchemix `VotingEscrow` report.

### Finding Description
`Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` [1](#0-0) . `addToDepositTokensOfAccount` is callable only by a registered deposit token, but it is invoked inside `DepositToken._transfer` on every 0→non-zero recipient balance [2](#0-1)  and inside `_mint` on first deposits [3](#0-2) . Since the check runs inside the transfer/mint, the revert propagates and the entire deposit or transfer fails.

Attack path (all public entry points, no privileges):
1. For each deposit token registered in the pool, the attacker deposits a dust amount of underlying via `Pool.deposit` (or acquires msdTOKEN any way), receiving msdTOKEN.
2. The attacker calls `DepositToken.transfer(victim, dust)` for each token. Each transfer adds that token to `depositTokensOfAccount[victim]`.
3. Once the combined list reaches 30, the victim's own `Pool.deposit` into any collateral they don't already hold reverts in `addToDepositTokensOfAccount`, as does anyone sending them a new deposit token.

Debt tokens count toward the same limit, so a victim who already holds N positions only needs 30−N dusted tokens.

### Impact Explanation
Griefing / temporary freezing: the victim cannot open positions in new collateral types and cannot receive deposit-token transfers of tokens not already in their list, until they manually clear entries. Recovery is possible — the victim can transfer each dust balance out (balance → 0 triggers `removeFromDepositTokensOfAccount` [4](#0-3) ) — so this is a temporary DoS rather than permanent freezing, and the attacker's cost scales with the number of distinct registered deposit tokens. If the pool registers fewer than 30 combined tokens, the attack is impossible on that deployment; that deployment-specific condition could not be fully confirmed from the indexed code.

### Likelihood Explanation
Low-to-moderate. Requires the pool to have ~30 registered deposit+debt tokens and the attacker to acquire a balance in each (each requires real collateral deposit, though only dust). The victim can self-recover by emptying dust entries, so impact is bounded. No modifier (`onlyIfDepositTokenIsActive`, `nonReentrant`, pause flags) prevents the dust transfers themselves on an active pool.

### Recommendation
- Decouple list membership from transfers: only add to `depositTokensOfAccount` on explicit deposits, or allow the account to opt out / prune arbitrary entries.
- Alternatively, enforce a minimum first-transfer amount (dust threshold) before list insertion, or let anyone call a `purgeToken(account, token)` that removes an entry when `balanceOf(account) < threshold`.

### Proof of Concept
Foundry/Hardhat fork outline against a deployed `Pool`:

```ts
// For each registered deposit token dt_i in pool.getDepositTokens():
// 1. attacker approves underlying and calls pool.deposit(dt_i, dust)
// 2. attacker calls dt_i.transfer(victim, 1)
// After 30 - victimExistingTokens iterations:
// victim calls pool.deposit(newDepositToken, amount)
// => reverts with UserReachedMaxTokens via DepositToken._mint -> addToDepositTokensOfAccount
```

Existing test `should revert when reach max tokens` in `test/Pool.test.ts` demonstrates the revert condition [5](#0-4) . A reproducible PoC additionally requires enumerating real registered deposit tokens on a fork and funding the attacker with each underlying — I could not verify the deployed token count from the indexed sources, which is the main open condition for exploitability on each chain.

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

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
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
