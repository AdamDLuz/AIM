---
name: aim
description: Talk to Claude, Grok, or Codex on another computer on this LAN (Windows, Mac, or Ubuntu) through AIM (AI Messenger). Use when work must run on a different machine, a Mac or Ubuntu box should git pull and build, a binary or file needs to move between GitHub folders, or the user mentions aim, AI Messenger, or another computer.
user-invocable: true
---

# AIM

Each of the user's computers runs one daemon. It knows that computer's GitHub folder. Claude, Grok, and Codex on every machine share it. You are the agent on this computer. Name the other computer, then the agent there (`claude`, `grok`, or `codex`).

`aim task` runs a command on that machine now. The other agent's session can be closed. `aim send` only leaves mail until that agent runs `aim inbox`.

## Find aim

Use the first one that exists:

1. `aim`
2. `~/.aim/bin/aim` or `%USERPROFILE%\.aim\bin\aim.cmd`
3. From a checkout of this repo, with `PYTHONPATH` set to the repo `src` directory: `py -3 -m aim` on Windows, `python3 -m aim` on Mac and Ubuntu.

Pass `--json` on every command. Pass `--from claude`, `--from grok`, or `--from codex` — whichever you are. `AIM_AGENT` is the fallback.

## See the machines

```
aim --json status
aim --json peers
aim --json repos --peer PEER
```

If status says the daemon is down, start `aim serve` in the background, then check status again.

`peers` lists each machine's `github_root`. A `--cwd` of `MyApp` means `<that machine's github_root>/MyApp`. Take the folder name from `aim repos --peer PEER`. If the machine is missing and the user gave you its LAN address: `aim peers add HOST`.

## Run work there and bring the build back

```
aim --json task adam-mac --cwd MyApp --from claude --to claude --wait --timeout 900 --return-file dist/MyApp --fetch-to incoming --shell "git pull && make"
```

- `--cwd` is relative to the peer's github root.
- `--shell` is one command in that machine's shell (`cmd.exe` on Windows, `/bin/sh` on Mac and Ubuntu). Use it when the command contains `&&`.
- Or put the argv after `--`: `aim --json task adam-mac --cwd MyApp --from claude --wait -- git pull`
- Pass `--shell` or a command after `--`, not both.
- `--return-file` is relative to `--cwd`. Repeat the flag for more files. A directory comes back as a zip. `--fetch-to` is a directory on this computer.
- `--wait` exits 1 when the remote command exits non-zero or times out. Read `task.output` and `task.exit_code` in the JSON.
- `--to claude` delivers the completion note to Claude's inbox on that machine. The command still runs if that session is closed.

## Mail

```
aim --json send adam-mac --from claude --to claude --message "build is starting"
aim --json inbox --agent claude --wait 20
aim --json ack MESSAGE_ID
```

Reply with `aim send` to the message's `from_node`. `--to *` is any agent on that machine. Ack ids after you have acted on them.

## Files

```
aim --json send-file adam-ubuntu .\notes.txt --dest MyApp/incoming/notes.txt --from claude
aim --json pull adam-mac --path MyApp/dist/MyApp.zip --output incoming/MyApp.zip
aim --json fetch-file adam-mac FILE_ID --output incoming/MyApp.zip
```

`--dest` and `--path` are relative to the other machine's github root. `pull` of a directory saves a zip. `fetch-file` downloads an id from `task.result_files`.

## Setup

Install or pair only when the user asks. Windows: `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`. Mac or Ubuntu: `sh scripts/install.sh`. A later machine joins with `aim install --secret-file <pair file> --name <machine>`.

Do not read `config.json` or `pair.json`, do not print the shared secret, and do not run `aim pair-export`. The secret is permission to run commands as the daemon's user on the LAN. File paths the other machine chooses stay inside its GitHub folder.
