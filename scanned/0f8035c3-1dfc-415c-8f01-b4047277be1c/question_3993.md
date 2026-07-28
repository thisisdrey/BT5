# Q3993: Smart account deployment and initialization: nonce race / unexpected privilege / one-shot configure

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with deployment timing around the deployer-specific nonce and predicted address observation while a second configuration attempt is made against a Safe that already ran initialization once and make duplicate or crafted initializer arrays install more delegate or allowance surface than intended, breaking the rule that `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe and leading to Unauthorized delegate or spending surface on a deployed smart account?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: deployment timing around the deployer-specific nonce and predicted address observation
- Exploit idea: make duplicate or crafted initializer arrays install more delegate or allowance surface than intended
- Invariant to test: `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe
- Expected Immunefi impact: Unauthorized delegate or spending surface on a deployed smart account
- Fast validation: Fuzz initializer arrays and duplicate entries, then assert the resulting Safe has exactly the declared owners, threshold, delegates, and routes.
