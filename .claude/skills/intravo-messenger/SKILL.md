---
name: intravo-messenger
description: Talk to Claude, Grok, or Codex on another computer on this LAN (Windows, Mac, or Ubuntu) through Intravo Messenger (ivm). Use when work must run on a different machine, a Mac or Ubuntu box should git pull and build, a binary or file needs to move between GitHub folders, or the user mentions ivm, another computer, or Intravo Messenger.
user-invocable: true
---

# Intravo Messenger

Each of the user's computers runs one daemon. It knows that computer's GitHub folder. Claude, Grok, and Codex on every machine share it. You are the agent on this computer. Name the other computer, then the agent there (`claude`, `grok`, or `codex`).

`ivm task` runs a command on that machine now. The other agent's session can be closed. `ivm send` only leaves mail until that agent runs `ivm inbox`.

## Find ivm

Use the first one that exists:

1. `ivm`
2. `~/.intravo-messenger/bin/ivm` or `%USERPROFILE%\.intravo-messenger\bin\ivm.cmd`
3. From a checkout of this repo, with `PYTHONPATH` set to the repo `src` directory: `py -3 -m intravo_messenger` on Windows, `python3 -m intravo_messenger` on Mac and Ubuntu.

Pass `--json` on every command. Pass `--from claude`, `--from grok`, or `--from codex` — whichever you are. `IVM_AGENT` is the fallback.

## See the machines

```
ivm --json status
ivm --json peers
ivm --json repos --peer PEER
```

If status says the daemon is down, start `ivm serve` in the background, then check status again.

`peers` lists each machine's `github_root`. A `--cwd` of `MyApp` means `<that machine's github_root>/MyApp`. Take the folder name from `ivm repos --peer PEER`. If the machine is missing and the user gave you its LAN address: `ivm peers add HOST`.

## Run work there and bring the build back

```
ivm --json task adam-mac --cwd MyApp --from claude --to claude --wait --timeout 900 --return-file dist/MyApp --fetch-to incoming --shell "git pull && make"
```

- `--cwd` is relative to the peer's github root.
- `--shell` is one command in that machine's shell (`cmd.exe` on Windows, `/bin/sh` on Mac and Ubuntu). Use it when the command contains `&&`.
- Or put the argv after `--`: `ivm --json task adam-mac --cwd MyApp --from claude --wait -- git pull`
- Pass `--shell` or a command after `--`, not both.
- `--return-file` is relative to `--cwd`. Repeat the flag for more files. A directory comes back as a zip. `--fetch-to` is a directory on this computer.
- `--wait` exits 1 when the remote command exits non-zero or times out. Read `task.output` and `task.exit_code` in the JSON.
- `--to claude` delivers the completion note to Claude's inbox on that machine. The command still runs if that session is closed.

## Mail

```
ivm --json send adam-mac --from claude --to claude --message "build is starting"
ivm --json inbox --agent claude --wait 20
ivm --json ack MESSAGE_ID
```

Reply with `ivm send` to the message's `from_node`. `--to *` is any agent on that machine. Ack ids after you have acted on them.

## Files

```
ivm --json send-file adam-ubuntu .\notes.txt --dest MyApp/incoming/notes.txt --from claude
ivm --json pull adam-mac --path MyApp/dist/MyApp.zip --output incoming/MyApp.zip
ivm --json fetch-file adam-mac FILE_ID --output incoming/MyApp.zip
```

`--dest` and `--path` are relative to the other machine's github root. `pull` of a directory saves a zip. `fetch-file` downloads an id from `task.result_files`.

## Setup

Install or pair only when the user asks. Windows: `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`. Mac or Ubuntu: `sh scripts/install.sh`. A later machine joins with `ivm install --secret-file <pair file> --name <machine>`.

Do not read `config.json` or `pair.json`, do not print the shared secret, and do not run `ivm pair-export`. The secret is permission to run commands as the daemon's user on the LAN. File paths the other machine chooses stay inside its GitHub folder.
