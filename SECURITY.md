# Security

## What this tool does

`agent-canary` writes small repositories containing harmless instructions ("create an empty file named ...") and runs **your** agent command inside them. It then checks whether the agent created the files.

The injected instructions never ask for anything except an empty file in a temporary directory. The command you pass with `--cmd` is a real agent, though, and it may act on whatever it reads.

## Use it safely

- Run it in a disposable environment: a container, a VM, or a throwaway user account.
- Do not give the agent real credentials. By default only `PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM` and `TMPDIR` are passed. Use `--pass-env NAME` for one variable at a time, and prefer a dedicated low-privilege token.
- `HOME` is passed because most agents keep their configuration there. Anything in that home directory is reachable by the agent, so use a dedicated user.
- Do not replace the canary instructions with real exfiltration payloads.
- Test only agents and systems you are allowed to test.

## Reporting a vulnerability

Open a private security advisory on the repository, or contact the maintainer through the email on their GitHub profile. Please include the version and steps to reproduce.
