[File: internal/git/gitattributes/check_attr.go -> Scope: Advanced DoS] [Function: CheckAttrCmd.Check path validation] `Check` only rejects paths containing a literal NUL byte, then writes `path + \
