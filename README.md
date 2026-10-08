# AIM

AI Messenger is a local-network messenger for Claude, Grok, and Codex. One daemon on each of your Windows, Mac, and Ubuntu computers knows that computer's GitHub folder, runs commands there, and moves files between the machines.

An agent on one computer can ask another to `git pull` and build, then take the compiled result back. `aim task` runs even when the agent session on the other computer is closed. `aim send` leaves mail until that agent reads `aim inbox`.

Humans can use the same commands. Run `aim --help` for the full list. Agents should pass `--json`.

## Example

Windows asks the Mac to update a repo, build, and send the result back:

```
aim peers
aim task adam-mac --cwd MyApp --from claude --to claude --wait --timeout 900 --return-file dist/MyApp --fetch-to incoming --shell "git pull && make"
```

`adam-mac` is the name chosen when that computer was installed. `--cwd` and `--return-file` are relative to the Mac's GitHub folder, which `aim peers` prints. A directory comes back as a zip into `incoming` on the Windows machine.

To leave a note for Claude on that Mac instead of running a command:

```
aim send adam-mac --from claude --to claude --message "pulled the build"
```

Claude on the Mac reads it with `aim inbox --agent claude`.

## Install

Requires Python 3.11 or newer. The program uses the standard library only.

From a checkout of this repo on Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

On Mac or Ubuntu:

```
sh scripts/install.sh
```

Add `--name adam-win` (or `adam-mac`, `adam-ubuntu`) so the other machines see a stable name. Add `--github-root` when the GitHub folder is somewhere else. The installer otherwise looks for `Documents/GitHub`, `GitHub`, `source/repos`, `Projects`, `code`, or `src` under your home directory.

Install registers the `aim` command, copies the agent skill for Claude, Codex, and Grok, and starts the daemon. On Windows it registers a logon task. If Task Scheduler refuses that task, the installer puts `AIM.vbs` in the user Startup folder instead. New terminals pick up `aim` after the user PATH change. On Mac the installer writes that PATH line to both `~/.zprofile` and `~/.zshrc`, and links the command at `~/.local/bin/aim`. On Ubuntu it writes `~/.profile` and `~/.bashrc` and makes the same link.

Config lives at `%USERPROFILE%\.aim\config.json` on Windows and `~/.aim/config.json` on Mac and Ubuntu. The shared key is in that file. Re-running install keeps the same key.

## Pair the other computers

On the first computer:

```
aim pair-export
```

That writes a pair file (by default `~/.aim/pair.json`). Copy the file to the other computer yourself. Do not paste it into a chat, commit it, or email it.

On each other computer, from a checkout of this repo:

```
sh scripts/install.sh --secret-file /path/to/pair.json --name adam-mac
```

Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 --secret-file C:\path\to\pair.json --name adam-ubuntu
```

Use a different `--name` on every machine. After that, `aim peers` on any of them should list the others. If a machine stays missing, broadcast is blocked (Wi-Fi client isolation or a firewall). Pin the address:

```
aim peers add 192.168.1.20
```

## Firewall

The daemon listens on TCP 4777. Discovery uses UDP 4778. Both are inbound on private networks only.

Windows, in an elevated PowerShell:

```
New-NetFirewallRule -DisplayName "AI Messenger" -Direction Inbound -Protocol TCP -LocalPort 4777 -Action Allow -Profile Private
New-NetFirewallRule -DisplayName "AI Messenger Discovery" -Direction Inbound -Protocol UDP -LocalPort 4778 -Action Allow -Profile Private
```

If Windows asks whether to allow Python, allow it on private networks. The installer does not add these rules.

Ubuntu, only when ufw is enabled:

```
sudo ufw allow from 192.168.0.0/16 to any port 4777 proto tcp
sudo ufw allow from 192.168.0.0/16 to any port 4778 proto udp
sudo ufw allow from 10.0.0.0/8 to any port 4777 proto tcp
sudo ufw allow from 10.0.0.0/8 to any port 4778 proto udp
```

macOS may ask for local network access the first time the daemon binds. Allow it.

## Security

The shared key is permission to run commands as the user who started the daemon, on every computer that has it. The daemon rejects clients that are not on a local network (loopback, private, or link-local). Uploads, downloads, and files returned from a task must stay inside that machine's GitHub folder. A shell command itself is not sandboxed.

Do not commit `config.json` or `pair.json`.

## Check one machine

```
aim status
aim whoami
aim self-test
```

`aim whoami` prints the node name, the operating system, and the GitHub folder. It does not print the key. `aim self-test` sends a local message and runs `echo aim-ok` on this computer. It does not prove that the Mac or the Ubuntu computer can see this one. After those computers are paired, `aim peers` should show them online.
