import json
import os

MAX_REPO = 25
SOURCE_REPO = 'tare-io/tare-contracts'
REPO_NAME = 'tare-contracts'
run_number = os.environ.get("GITHUB_RUN_NUMBER") or os.environ.get(
    "CI_PIPELINE_IID", "0"
)


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index."""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "repositories.json"
    )
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    'tare-io__tare-contracts/contracts/interfaces/Accounts.sol',
    'tare-io__tare-contracts/contracts/interfaces/IERC1404.sol',
    'tare-io__tare-contracts/contracts/interfaces/ILoansExchange.sol',
    'tare-io__tare-contracts/contracts/interfaces/ILoansNFT.sol',
    'tare-io__tare-contracts/contracts/interfaces/ILoans.sol',
    'tare-io__tare-contracts/contracts/interfaces/INavCalculator.sol',
    'tare-io__tare-contracts/contracts/interfaces/IPortfolioVault.sol',
    'tare-io__tare-contracts/contracts/interfaces/ISmartAccountFactory.sol',
    'tare-io__tare-contracts/contracts/interfaces/ITrustedCalls.sol',
    'tare-io__tare-contracts/contracts/interfaces/ITrustedSpender.sol',
    'tare-io__tare-contracts/contracts/interfaces/IVaultShareToken.sol',
    'tare-io__tare-contracts/contracts/interfaces/LedgerEntries.sol',
    'tare-io__tare-contracts/contracts/LoansExchange.sol',
    'tare-io__tare-contracts/contracts/LoansLedger.sol',
    'tare-io__tare-contracts/contracts/LoansNFT.sol',
    'tare-io__tare-contracts/contracts/Loans.sol',
    'tare-io__tare-contracts/contracts/misc/GuardianAccessControl.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/IERC7540.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/IERC7575.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/IGuardianAccessControl.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/ILoansAuth.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/IModuleManager.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/IRescuable.sol',
    'tare-io__tare-contracts/contracts/misc/interfaces/ISafe.sol',
    'tare-io__tare-contracts/contracts/misc/LoansAuth.sol',
    'tare-io__tare-contracts/contracts/misc/Rescuable.sol',
    'tare-io__tare-contracts/contracts/NavCalculator.sol',
    'tare-io__tare-contracts/contracts/PortfolioVault.sol',
    'tare-io__tare-contracts/contracts/SmartAccountFactory.sol',
    'tare-io__tare-contracts/contracts/TrustedCalls.sol',
    'tare-io__tare-contracts/contracts/TrustedSpender.sol',
    'tare-io__tare-contracts/contracts/VaultShareToken.sol',
]

target_scopes = [
    "Critical. Unauthorized theft, diversion, or reassignment of USDC, vault assets, vault shares, loan cashflows, or loan NFTs belonging to another user or to shared protocol state through an unprivileged path in the scoped contracts.",
    "Critical. Permanent or practically unrecoverable lock of USDC, claimable vault assets, loan cashflows, vault shares, or loan NFTs for honest users or the vault caused by an unprivileged path, including bricked async requests, stuck listed loans, or unclaimable withdrawals.",
    "High. Unauthorized role, whitelist, controller, receiver, delegate, allowance, approval, lock, or ownership bypass that lets an unprivileged attacker execute actions or move value on behalf of another investor, borrower, seller, buyer, vault shareholder, or Safe account.",
    "High. Material corruption of per-loan ledger balances, cash segregation, investor entitlement, vault NAV, async deposit or redeem accounting, offer settlement accounting, or share pricing that lets an unprivileged attacker extract value, shift value between users, or create underbacked claims.",
    "High. Material production denial of service or forced bad state in Loans, PortfolioVault, LoansExchange, SmartAccountFactory, TrustedCalls, TrustedSpender, or the token contracts, triggered by an unprivileged user and blocking funding, disbursement, trading, claiming, withdrawing, or NAV-sensitive approvals across users.",
]

TARE_ALLOWED_IMPACT_SCOPE = """## Tare Allowed Impact Gate
Only accept impacts supported by this repository and the current public audit scope:
- Theft, diversion, or unauthorized reassignment of USDC, vault assets, vault shares, loan cashflows, or loan NFTs from honest users or shared protocol state.
- Permanent or practically unrecoverable lock of USDC, claimable assets, shares, cashflows, or loan NFTs caused by an unprivileged path.
- Unauthorized state transition or permission bypass that lets an unprivileged actor act for another investor, borrower, seller, buyer, shareholder, Safe, controller, receiver, or listed-loan owner.
- Material corruption of ledger balances, per-loan cash segregation, investor entitlement, vault NAV, async request accounting, or offer settlement that produces real value loss, unfair mint or redeem, or underbacked claims.
- Material production DoS or state corruption that blocks funding, disbursement, withdrawals, claims, NAV-sensitive approvals, or loan settlement across users.
Out of scope: guardian, admin, pauser, originator, servicer, portfolio manager, investor manager, whitelister, calculating agent, or other privileged-role abuse; malicious peers, nodes, tokens, oracles, or off-chain systems; accepted known issues and trust assumptions in SECURITY.md; attacks that only harm the attacker's own position; tests, mocks, scripts, docs, readmes, generated files, deployment artifacts, `.toml`, style issues, event-only mismatches, minor rounding without value extraction, and dependency-only claims without a repository root cause."""

TARE_AUDIT_PIVOTS = """## Smart Audit Pivots
- Loans path: `create`, `fund`, `disburse`, payment, waterfall, refund, withdraw, and NFT transfer or lock flows must preserve per-loan cash segregation, correct investor ownership, and role-bound value movement.
- Vault path: async request, approve, claim, cancel, loan curation, cashflow collection, and NAV update flows must preserve whitelist gating, freshness, counter conservation, and fair share pricing.
- Exchange path: offer creation, acceptance, cancellation, and lock routing must preserve designated-buyer settlement, correct NFT ownership, correct cash routing, and no stuck listed loans or trapped cashflows.
- Smart-account path: Safe deployment, module enablement, delegate registration, approvals, route allowances, and trusted-call surfaces must never let an unprivileged actor execute or spend from another Safe."""


def question_generator(target_file: str) -> str:
    """
    Generate security questions for one Tare target.
    """

    prompt = f"""
    Generate Tare smart-contract security questions for this exact target file:

    {target_file}

    Project lens:
    Tare lending, vault, exchange, and Safe-account contracts. Focus on unprivileged external entrypoints: borrower, investor, seller, buyer, vault shareholder, ordinary caller, or an account controlling only its own wallet, loan, request, or listed position.

    Impact gate:
    {TARE_ALLOWED_IMPACT_SCOPE}

    {TARE_AUDIT_PIVOTS}

    Rules:
    * Treat `File Name:` as the exact file and `Scope:` as the only impact.
    * Assume repository context is available; do not ask for more code.
    * The attacker is strictly unprivileged. Do not assume guardian, admin, pauser, originator, servicer, portfolio manager, investor manager, whitelister, calculating agent, deployer, leaked keys, database control, or infrastructure control.
    * Do not base questions on malicious peers, nodes, tokens, or off-chain systems.
    * Respect the current trust model and known issues in `SECURITY.md`.
    * Exclude tests, mocks, scripts, docs, readmes, generated files, deployment artifacts, `.toml`, event-only mismatches, style issues, and minor rounding without extractable value.
    * A borrower, investor, seller, or shareholder acting in their own position is only interesting if they can steal from, lock, or corrupt value for another user or shared protocol state.
    * Focus on the current USDC-only production design and the scoped Solidity files.
    * Generate 14 to 18 high-signal questions with non-overlapping root causes.
    * Name the exact corrupted value: USDC balance, `ACC_CASH`, payable or receivable balance, loan NFT owner, unlocker, offer record, pending request amount, claimable shares, claimable assets, `lastNav`, `pendingNav`, `navCursor`, `ownershipNonce`, delegate flag, approval, allowance, or Safe execution authority.
    * Every question must be testable with a focused Foundry unit, integration, or fuzz test.

    Each question must include target symbol, attacker-controlled input, required state, call path, broken invariant, corrupted value, scoped impact, and proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Symbol: symbol_or_contract] Can attacker-controlled INPUT under REQUIRED_STATE reach CALL_PATH and break INVARIANT, corrupting EXACT_VALUE with scoped impact SCOPE_IMPACT? Proof idea: write a focused Foundry test that drives ENTRYPOINT through the vulnerable path and asserts EXPECTED_SAFETY_PROPERTY.",
    ]
    """
    return prompt


def audit_format(question: str) -> str:
    """
    Generate a focused Tare exploit-question validation prompt.
    """
    return f"""# TARE QUESTION REVIEW

## Exploit Question
{question}

## Scope Rules
- Audit only scoped Tare production contracts in this repository under the current public contest or bounty lens.
- Ignore privileged-role abuse, malicious peers, nodes, tokens, oracles, off-chain operators, tests, mocks, scripts, docs, readmes, and generated artifacts.
- Do not ask for repo contents or claim files are missing.

## Objective
Decide whether the question leads to a real Tare vulnerability. The attacker must be unprivileged and must enter through a production contract path available to an ordinary borrower, investor, seller, buyer, vault shareholder, or caller.

Reject claims that need guardian, admin, servicer, originator, portfolio-manager, investor-manager, whitelister, or calculating-agent powers. Reject issues that only let the attacker harm their own loan, request, Safe, or listed position without harming another user or shared protocol state. Prefer #NoVulnerability unless the path proves theft, durable lock, permission bypass, material accounting corruption, NAV abuse with value impact, or material cross-user DoS.

## Required Impacts
{TARE_ALLOWED_IMPACT_SCOPE}

{TARE_AUDIT_PIVOTS}

## Method
1. Trace the unprivileged entrypoint.
2. Map it to exact scoped files and functions.
3. Follow the full path through permissioning, accounting, token movement, and final stored or transferred effect.
4. Identify the exact corrupted value and the concrete user or protocol harm.
5. Reject if existing guards preserve the invariant or the impact is out of scope or immaterial.

## Reject Immediately
- Any dependence on guardian, admin, pauser, originator, servicer, portfolio manager, investor manager, whitelister, or calculating agent authority.
- Malicious peer, node, token, oracle, bridge, price feed, off-chain ledger, ACH, bank, or ops-process assumptions.
- Issues rooted only in tests, mocks, scripts, docs, readmes, generated files, deployment artifacts, or `.toml`.
- Event-only mismatches, style issues, accepted known issues, or rounding noise without real extractable value or durable lock.

## Output
If valid:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If invalid, output exactly:
#NoVulnerability found for this question.
"""


def scan_format(report: str) -> str:
    """
    Generate a cross-project analog scan prompt for Tare issues.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Task
Use the external report only as a bug-class seed. Search this Tare repository for a native analog with the same root cause in scoped loans, vault, exchange, Safe-account, or token flows.

## Required Impacts
{TARE_ALLOWED_IMPACT_SCOPE}

{TARE_AUDIT_PIVOTS}

Report only if this repository has its own reachable root cause, unprivileged trigger, broken invariant, exact corrupted value, and matching target scope or allowed impact. Reject privileged assumptions, malicious tokens or off-chain operators, external-system-only issues, dependency-only behavior, and anything excluded by the Tare audit scope.

## Work Plan
1. Classify the external bug into one Tare invariant.
2. Map it to exact scoped files and functions.
3. Trace attacker input through production permissioning, accounting, and value movement.
4. Identify the wrong balance, owner, unlocker, request counter, claimable amount, NAV field, allowance, or execution authority.
5. Reject if existing guards preserve the invariant or the impact is not material or in scope.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict Tare validation prompt for security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim against scoped Tare production contracts in this repository under the current public audit scope.
- Do not invent a stronger claim, change target scope, or upgrade severity without evidence.
- A valid issue must be triggered by an unprivileged external attacker using only production inputs exposed by the scoped contracts.
- Reject guardian, admin, pauser, originator, servicer, portfolio manager, investor manager, whitelister, calculating agent, deployer, or leaked-key assumptions.
- Reject malicious peer, node, token, oracle, bridge, price-feed, bank, ACH, or off-chain operator assumptions.
- Reject accepted known issues or trust assumptions in `SECURITY.md`.
- Reject attacks that only worsen the attacker's own loan, request, sale offer, Safe, or vault position without harming another user or shared protocol state.
- Reject tests, mocks, scripts, docs, readmes, generated files, deployment artifacts, `.toml`, event-only mismatches, style issues, and dependency-only bugs.
- The final impact must match one `target_scopes` item or the allowed-impact gate below and must identify the exact corrupted value.

## Required Impacts
{TARE_ALLOWED_IMPACT_SCOPE}

{TARE_AUDIT_PIVOTS}

## Required Checks
1. Exact file and function references in scoped code.
2. A clear Tare invariant tied to value custody, authorization, ledger integrity, vault accounting, exchange settlement, or Safe execution.
3. A reachable exploit path: preconditions -> attacker input -> production call path -> bad value.
4. Existing guards reviewed and shown insufficient.
5. Exact wrong value named: USDC balance, `ACC_CASH`, receivable or payable balance, loan NFT owner, unlocker, offer record, request counter, claimable shares, claimable assets, `lastNav`, `pendingNav`, `navCursor`, delegate flag, allowance, approval, or Safe execution authority.
6. A reproducible proof path via Foundry unit, integration, or fuzz-style testing.

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete allowed repository impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt
