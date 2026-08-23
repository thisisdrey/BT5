[File: internal/git/updateref/updateref.go -> Function: Updater.setState] Since `setState` reads exactly one line via `u.stdout.ReadString('\\n')` and compares it literally to `fmt.Sprintf(\
