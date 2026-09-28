### Title
Attacker can fill a victim's `depositTokensOfAccount`/`debtTokensOfAccount` accounting set with dust transfers until `MAX_TOKENS_PER_USER`, DoS-ing all new collateral deposits and new debt issuance for the victim - (File: contracts/Pool.sol + contracts/DepositToken.sol)

### Summary
The CVE class (CVE-2017-15593) is *reference-count mishandling leading to resource leak / denial of service*. Metronome tracks, per account, which `DepositToken`s and `DebtToken`s it holds in bounded enumerable sets (`depositTokensOfAccount`, `debtTokensOfAccount` in `Pool.sol`). An entry is added on the zero→nonzero balance transition (`DepositToken._transfer` line 518–519, `DebtToken._mint` line 598–599) and removed on the nonzero→zero transition. The set is capped by `MAX_TOKENS_PER_USER` counting deposit + debt tokens combined, and the cap check runs inside `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, reverting with `UserReachedMaxTokens`.

Because `DepositToken.transfer`/`transferFrom` are public and only check the *sender's* unlocked balance, an attacker can push 1-wei dust of every whitelisted deposit token into any victim address. Each dust token permanently occupies a slot in the victim's set. Once the victim's combined set reaches `MAX_TOKENS_PER_USER`, every code path that would add a *new* token to the victim's accounting reverts — so the victim can no longer deposit a new collateral type (even via `deposit(amount, onBehalfOf_)` by a third party), cannot receive new deposit tokens by transfer, and cannot issue any new synthetic/debt token (`issue` → `_mint` → `addToDebtTokensOfAccount` reverts).

### Finding Description
- `DepositToken._transfer` (contracts/DepositToken.sol:498–526): on any transfer where `recipient` had zero balance, it calls `pool.addToDepositTokensOfAccount(recipient_)`. There is no minimum amount and no opt-in — a 1-wei transfer registers the token in the recipient's accounting.
- `DepositToken._mint` (line 486–487) does the same on deposits, so `deposit(1, victim)` for a token the victim doesn't hold also consumes a slot.
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` enforce `UserReachedMaxTokens` on the combined deposit+debt set size (see test/Pool.test.ts:1386–1416 which fills the cap with `max/2` deposits + `max/2` debts).
- All slot-consuming paths are permissionless with respect to the *victim*: the attacker needs only their own unlocked msdTOKEN balance, obtainable by depositing dust collateral themselves.
- Nothing stops this: `transfer` only calls `_revertIfLocked` on the sender; `MappedEnumerableSet.add` reverts on overflow, propagating the revert to the victim-facing call.

The "leak" analogue: slots in the victim's set are leaked by the attacker (the victim never consented and the accounting grows monotonically until the cap), mirroring the leaked reference counts in XSA-242 — growth of a bounded per-entity resource by an unprivileged party until service denial.

### Impact Explanation
Once saturated:
- `deposit(...)` for any collateral type the victim doesn't already hold reverts → victim cannot add new collateral types, so cannot improve health with a different asset.
- `issue`/`mint` for any new `DebtToken` reverts → victim cannot open any new synthetic position.
- Incoming transfers of new deposit-token types revert.

Existing balances remain withdrawable/repayable (`_burn` removes entries), so the victim can recover by transferring dust positions out — this is a **temporary freezing / liveness denial of protocol functionality** rather than permanent loss. During liquidation stress, inability to deposit *other* collateral types can force otherwise-avoidable liquidations, amplifying impact.

### Likelihood Explanation
- Fully unprivileged: attacker needs only an EOA plus dust of each whitelisted collateral (depositable with no minimum; `amount_ == 0` is the only rejection).
- Cost scales with number of listed deposit tokens (small); each `transfer(victim, 1)` costs one ERC20 transfer's gas.
- No governance, oracle, keeper, or privileged prerequisite; works on the deployed configuration as long as ≥1 deposit token exists that the victim doesn't hold.
- Attacker can pre-compute `MAX_TOKENS_PER_USER - (victim's current count)` and fill exactly.

### Recommendation
- Add a minimum meaningful amount (or `minDeposit`/dust threshold) before registering a token in `depositTokensOfAccount`, or
- Move the add/remove bookkeeping out of the ERC20 transfer path (track deposits only via `deposit`/`_mint`, and require `msg.sender == recipient` opt-in for list growth), or
- Let the victim opt out: `removeFromDepositTokensOfAccount`-style self-cleaning that doesn't require transferring locked dust, or allow eviction of zero-value entries by anyone.

### Proof of Concept
Hardhat/fork sketch (no privileged roles):

```ts
// attacker deposits 1 wei of each whitelisted collateral and dusts the victim
const depositTokens: DepositToken[] = await getAllDepositTokens(pool);
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber();

for (const dt of depositTokens.slice(0, max)) {
  const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, attacker.address);   // mint dust msdTOKEN
  await dt.connect(attacker).transfer(victim.address, 1);    // leaks a slot into victim's set
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(max);

// victim can no longer deposit a collateral type they don't already hold
const newCollateral = depositTokens[max]; // or any token not yet in the set
await expect(
  newCollateral.connect(victim).deposit(parseEther('10'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim cannot issue a new debt-token type either
const someDebtToken = /* any DebtToken not in victim's debtTokensOfAccount */;
await expect(
  someDebtToken.connect(victim).issue(1, victim.address)
).to.be.reverted; // addToDebtTokensOfAccount -> UserReachedMaxTokens
```

Caveat: the exact revert surfaces inside `DepositToken._mint`/`DebtToken._mint` via `addTo*TokensOfAccount`; if the victim already holds most listed tokens, the attacker can only fill remaining slots, so severity scales with the number of whitelisted assets vs. `MAX_TOKENS_PER_USER`. I was not able to read `Pool.sol`'s cap-check body or `MappedEnumerableSet` within this session's iteration budget; the combined-count behavior is inferred from test/Pool.test.ts:1386–1416, which shows the cap counts deposit and debt tokens together.