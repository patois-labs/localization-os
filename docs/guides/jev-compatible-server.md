# Quality judges: hosted Jev, or a Jev-compatible server you run

This guide gets a **quality judge** working in Localization OS: the engine that scores every
translated segment for a workflow step's QE routing gate, and (when you turn it on) drives the
auto-fix loop. Every part starts with the click path in the app; the commands come after.

To remove a saved key: Setup AI -> engine Edit -> Remove key -> tick confirmation -> Save.
Blank or whitespace key inputs keep the existing key. Hosted Jev is disabled after removal;
a local judge becomes keyless and warns when reached off loopback. CLI: `engine update ID
--clear-key`. API update: `{"clear_api_key": true}`. MCP never carries keys.

Base URLs cannot contain `user:password@`; put credentials in the API key field instead.
Existing stored URL credentials remain usable but are hidden in displays. Test and Inspect
warn so you can move those credentials to the key field and save a clean URL.

Inside the app, **Help** covers the same ground in short: *Engines & Setup AI -> Quality
judges*, *Inspecting a quality judge* and *Run your own Jev-compatible server*, and
*Workflows -> QE routing gate and quality judges* and *Auto-fix failures*. The Setup AI
**Quality judges** group links to each of them.

- [1. What Jev and a Jev-compatible server are](#1-what-jev-and-a-jev-compatible-server-are)
- [2. Hosted Jev: paste a key](#2-hosted-jev-paste-a-key)
- [3. SemIf on a GPU box](#3-semif-on-a-gpu-box)
- [4. SemIf on the same PC as Localization OS](#4-semif-on-the-same-pc-as-localization-os)
- [5. SemIf on another machine, over Tailscale](#5-semif-on-another-machine-over-tailscale)
- [6. See what the judge is asked and how it decides](#6-see-what-the-judge-is-asked-and-how-it-decides)
- [7. Turn on auto-fix for a step](#7-turn-on-auto-fix-for-a-step)
- [8. Kev and Laya](#8-kev-and-laya)
- [9. Troubleshooting](#9-troubleshooting)

## 1. What Jev and a Jev-compatible server are

**Jev** (by TypeSafe) is a typed judge. It does not write prose: for each segment it answers five
fixed questions (accuracy, fluency, terminology and style, each as a level from none to
critical, plus a yes-or-no "did the meaning change") with a probability for every answer.
Localization OS turns those answers into a 0 to 100 score and a pass, fail or undecided verdict
for the step.

A **Jev-compatible server** is any program that speaks the same protocol
(`GET /v1/models` and `POST /v1/systemone`). **SemIf** is an open-weight Jev clone that runs on
your own GPU through a small server called **semif-serve**. Localization OS talks to it exactly
like hosted Jev, just at a different address and for free.

| | Hosted Jev (TypeSafe) | SemIf on your GPU |
|---|---|---|
| Engine kind in Setup AI | Jev (TypeSafe, hosted) | Jev-compatible local server (SemIf, Kev) |
| Right gate call on the project's set B benchmark | 88% | 81% |
| Typical time per segment | about 0.5 s | about 0.8 s |
| Cost | a fraction of a cent per segment, counted against the spend cap | $0, never capped |
| Where your text goes | TypeSafe's API | only the machine you run it on |
| Setup | paste a key | a GPU box with about 10 GB of free video memory |

For comparison, Kev-4B (another clone) scored 70% on the same benchmark. The benchmark rows were
drafted by a model and not reviewed by linguists, so read these numbers as a proof of concept,
not as a guarantee for your content. Every clone's gap to Jev is on meaning errors (a wrong
number, an omission), which is exactly what a gate exists to catch.

## 2. Hosted Jev: paste a key

**In the app:**

1. Open **Setup AI** and scroll to **Engines**, group **Quality judges**.
2. If the install already has `TYPESAFE_API_KEY` set, you will see a button **Create a Jev
   engine from TYPESAFE_API_KEY**. Press it and you are done: the key is stored encrypted and
   never shown, and pressing it again creates nothing new.
3. Otherwise open **+ Add engine**, give it a name, pick kind **Jev (TypeSafe, hosted)** (listed
   under *Quality judges (QE gate only)*), paste your TypeSafe API key and press **Add engine**.
   The address is fixed to `https://api.typesafe.ai`, so the key can never be sent anywhere
   else. Leave the floors and the timeout blank to use the install defaults (timeout 10 s).
4. On the new card open **Edit** and press **Test**. It lists the models and asks one tiny
   question (a few tokens).

**Commands** (on the host):

```powershell
python -m app.cli engine import-jev-env
python -m app.cli engine create --name "Jev" --kind jev --secret api_key=...
python -m app.cli engine test 7
```

## 3. SemIf on a GPU box

You need an NVIDIA GPU with about 10 GB of free video memory, `git`, and
[uv](https://docs.astral.sh/uv/). The first start downloads the Qwen3.5-4B model (about 8 GB).

A note on licensing: semif-serve (github.com/dddanielliu/semif-serve) has no licence file.
SemIf underneath is MIT. Localization OS ships no model code; you run the server yourself.

These are the commands, in PowerShell on Windows (the same steps work in a Linux shell):

```powershell
git -c "url.https://github.com/.insteadOf=git@github.com:" clone --recurse-submodules https://github.com/dddanielliu/semif-serve.git
cd semif-serve
uv sync
uv pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cu128
uv run --no-sync python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

The `-c "url...insteadOf..."` part matters: semif-serve's SemIf submodule is listed with an SSH
address (`git@github.com:...`), so a plain `git clone --recurse-submodules` fails unless this
machine has a GitHub SSH key. The setting makes git fetch it over HTTPS instead, for this one
command only. (If you already cloned without it, run the same `-c` setting with
`git submodule update --init --recursive` inside the folder.)

The last line must print a version ending in `+cu128` and `True`. `uv sync` installs the
CPU-only torch wheel on Windows, and the second install swaps in the CUDA build. Use the torch
version the project pins (2.10.0 when this was written). If it still says `+cpu`, run the install
again with `--reinstall`.

Optional, faster kernels: SemIf can use the `flash-linear-attention` and `causal-conv1d`
packages (`uv pip install flash-linear-attention causal-conv1d`). Without them it logs a
warning and uses a slower reference path. On Windows these often cannot be installed (they need
Triton), so the slow path is normal there and still answers in about 0.8 s per segment on an
RTX 3090.

Start the server:

```powershell
$env:CUDA_VISIBLE_DEVICES = "0"
$env:PYTORCH_ALLOC_CONF = "expandable_segments:True"
$env:SEMIF_PORT = "8077"
uv run --no-sync semif-serve
```

`PYTORCH_ALLOC_CONF=expandable_segments:True` is what the semif-serve README recommends: it lets
PyTorch grow its GPU memory in place instead of fragmenting it, which is what makes the model fit
on a 12 GB card. Keep it on larger cards too; it costs nothing.

`CUDA_VISIBLE_DEVICES` picks the GPU: SemIf needs exactly one visible CUDA device, so on a box
with two GPUs set it to the one with room for the model (`0` or `1`; `nvidia-smi` lists them).

**Always start it with `uv run --no-sync`.** A plain `uv run` re-syncs the environment and puts
the CPU torch wheel back, and the server then crawls on the CPU.

The settings it reads:

| Variable | Meaning |
|---|---|
| `SEMIF_HOST` | the address it listens on. Leave it unset for this PC only (loopback). |
| `SEMIF_PORT` | the port; this guide uses `8077`. |
| `SEMIF_API_KEY` | a shared secret. Required whenever it listens on anything but loopback. Use a long random value (at least 8 characters; 32 is better), for example the output of `[guid]::NewGuid().ToString("N")` in PowerShell. |
| `CUDA_VISIBLE_DEVICES` | which GPU SemIf uses (one only). |
| `PYTORCH_ALLOC_CONF` | set to `expandable_segments:True` (needed on 12 GB cards). |

Check it answers (in a second window):

```powershell
Invoke-RestMethod http://127.0.0.1:8077/v1/models
```

## 4. SemIf on the same PC as Localization OS

**In the app:**

1. **Setup AI** -> **Engines** -> **+ Add engine**.
2. Name it (for example "SemIf (this PC)"), kind **Jev-compatible local server (SemIf, Kev)**.
3. Base URL `http://127.0.0.1:8077`. Leave the API key blank unless you set `SEMIF_API_KEY`.
   Leave Model blank (it means `jev-latest`). Leave the floors blank to use the install defaults.
4. Press **Add engine**, then on its card **Edit** -> **Test**. You want "Jev reachable", a
   model list, and "one-question check OK".
5. Press **Inspect** on the card and use **Try it** with a sentence you know is wrong, to see the
   judge catch it (section 6).
6. Open the workflow, edit the Translate step, tick **QE routing gate**, and pick this engine as
   the **QE judge engine**. Save.

**Command:**

```powershell
python -m app.cli engine create --name "SemIf (this PC)" --kind jev_local --base-url http://127.0.0.1:8077
```

## 5. SemIf on another machine, over Tailscale

Use this when the GPU is in a different machine from Localization OS, for example a home GPU box
and Localization OS on this PC or on a cloud server.

**On the GPU box:**

1. Install Tailscale and sign in, so the box joins your tailnet. Note its tailnet address:
   `tailscale ip -4` prints a `100.x.y.z` address, and the admin console shows its MagicDNS name
   (`<host>.<tailnet>.ts.net`).
2. Start semif-serve listening on that tailnet address ONLY, with a key (the key is required
   off loopback). Use the exact `100.x.y.z` address `tailscale ip -4` printed; do not bind it
   to every interface, which would also expose it on your home or office network:

   ```powershell
   $env:PYTORCH_ALLOC_CONF = "expandable_segments:True"
   $env:SEMIF_HOST = "100.x.y.z"      # the address from `tailscale ip -4`
   $env:SEMIF_PORT = "8077"
   $env:SEMIF_API_KEY = "a-long-random-secret"   # pregate: allow-secret a placeholder to replace
   uv run --no-sync semif-serve
   ```

   The key is a password, **not transport encryption**: semif-serve speaks plain HTTP, so the
   text and the key are only protected because Tailscale encrypts everything on the tailnet.
   Keep the server on the tailnet address and never forward its port to the internet. A
   firewall rule on the GPU box that allows port 8077 only from the Tailscale interface is a
   prudent extra layer. Start semif-serve after Tailscale is connected: binding to the `100.x`
   address fails while that address does not exist yet.

**In the app:**

1. **Setup AI** -> **+ Add engine**, kind **Jev-compatible local server (SemIf, Kev)**.
2. Base URL `http://<host>.<tailnet>.ts.net:8077` or `http://100.x.y.z:8077` (use the IPv4
   `100.x` address; Tailscale's IPv6 addresses are not recognised as tailnet hosts).
3. Paste the same `SEMIF_API_KEY` value as the engine's API key. Without a key the app warns you:
   anyone who can reach the box could use it and read what is sent to it.
4. **Test**, then pick it as a step's QE judge engine as in section 4.

**Where Localization OS runs matters:**

- On a PC that is itself on the tailnet (Tailscale installed and signed in), the app reaches the
  box directly. Nothing else to set up.
- In a container on a cloud server, the `TS_AUTHKEY` secret starts a Tailscale sidecar in the container, and the
  app then sends ONLY engine calls to tailnet addresses (`100.64.0.0/10` and `*.ts.net`) through it; every
  cloud provider and connector call is untouched. Limit the sidecar's node with a Tailscale ACL to the one
  port you need. The sidecar settings are in [DEPLOY.md](../DEPLOY.md), and how the routing works is in
  [deployment.md](../architecture/deployment.md). For SemIf, allow
  port `8077` instead of LM Studio's `1234`.
- A GPU box on a network the app cannot reach at all will not work, whatever the URL.

## 6. See what the judge is asked and how it decides

**In the app:** Setup AI -> the judge's card -> **Inspect** (admins only). The page shows:

1. **What Jev is asked**: the five questions, what every level means, and the exact request body.
2. **How it decides**: the confidence floor, the meaning check, how "unsure" is handled, the score
   formula and the pass threshold, rendered from the code's own numbers and this engine's settings.
3. **Try it**: score one segment and see the request, the raw answer, each dimension's level, the
   verdict, the cost and the exact prompt the auto-fix post-editor would get.
4. **Ledger**: every call to this engine in the LLM Ledger (turn the Ledger on first).

Tries are limited to 20 a minute per person, one at a time per engine and one hosted try at a
time, and a try stops after the engine's timeout plus 2 seconds. These limits are kept inside
the running server, which fits the usual install (a single process on one machine). A CLI try
from another window, or a deploy spread over several machines, is not counted together with the
server's tries; for hosted Jev the spend cap is still checked before every call, so money stays
bounded.

**Commands:**

```powershell
python -m app.cli engine inspect 7
python -m app.cli engine jev-try 7 --source "Pay ten euros." --target "Zahlen Sie neun Euro." --source-locale en --target-locale de
python -m app.cli lqa jev-refine --yes --judge engine:7 --source "Pay ten euros." --target "Zahlen Sie neun Euro." --source-locale en --target-locale de
```

## 7. Turn on auto-fix for a step

Auto-fix hands a segment that fails the gate to an LLM **post-editor**, which sees the source,
every version so far and each Jev score and writes a new version; the same judge re-scores it.

**In the app:**

1. Open the workflow and edit the Translate step whose QE judge is a Jev engine.
2. Tick **Auto-fix failures** (the block appears only for a Jev judge).
3. Pick the post-editor. **Same as this step** uses the step's own engine; it **must be an LLM**
   (for example your LM Studio model). A machine-translation engine or a judge cannot post-edit,
   and saving refuses it.
4. Set the max rounds (1-5, other values are refused; default 3). Cross-check is optional. Save.

What to expect:

- A version that passes the gate replaces the text, and the segment passes. The editor badge
  reads *auto-fixed in N rounds*.
- Otherwise the segment goes to review with an *auto-fix tried N rounds* badge holding every
  round's score and text. The reviewer gets the best rewrite only when the judge was sure of it
  and it scores strictly higher than the original; otherwise the original stays.
- Confident major dimensions are auto-fixed even alongside an **unsure** dimension.
  Unsure dimensions are left unchanged and flagged for review. Without a confident major,
  an unsure failing segment goes to review without a rewrite. A segment at or above the threshold with an unsure dimension is *undecided*: not a
  pass, but not a fail either, so it carries on unchanged, is not sent to review, and counts as
  QE skipped (in the editor its *qe* badge reads "QE undecided" on hover).
- A step that redacts PII is not auto-fixed (a current limit), nor is output already withheld
  as a refusal. When the loop does not start, the badge reads *auto-fix skipped* with the reason.
- Every judge and post-editor call is in the LLM Ledger with its round number, and **Try it** on
  the judge's Inspect page shows the exact prompt the post-editor gets.
- A fully local loop (a local judge such as SemIf and a local post-editor such as LM Studio) is
  free and keeps running when the spend cap is reached. A loop that can cost money stops at the cap.

**Command:** `workflow edit-step` with `--qe-autofix`, `--qe-autofix-provider`,
`--qe-autofix-max-rounds` and `--qe-autofix-cross-check` (see [CLI.md](../CLI.md)).

## 8. Kev and Laya

- **Kev** (Kev-4B) speaks the same protocol, so it is added exactly like SemIf (kind
  Jev-compatible local server, its own URL and port). It holds about 15 GB of video memory, so it
  does not share a 24 GB card with a 12B LLM such as Gemma: with both loaded, requests slowed to
  the point of timing out.
- **Laya** does not speak the Jev protocol. It needs an adapter server in front of it, and it is
  not recommended for this job: it is trained on single-text business decisions, and on
  source-and-translation pairs it flagged almost every clean segment.

## 9. Troubleshooting

| What you see | What it means and what to do |
|---|---|
| "the server rejected the key; check the engine's API key" (HTTP 401/403) | The key is wrong or missing. For SemIf, the engine's API key must equal the server's `SEMIF_API_KEY`; for hosted Jev, paste the TypeSafe key again. |
| "timed out after N s; raise the engine's Timeout, seconds" | A local GPU can be slow on its first requests or while another model is busy. Edit the engine and raise **Timeout, seconds** (for example to 60). |
| "server not reachable (check it is running and reachable over the network/tailnet)" | A connection or proxy failed. Check that the server is running and the app can reach its address and port over the network/tailnet. |
| Test says the configured model is "NOT in the list" | Set the engine's Model to one of the names the Test lists, or leave it blank for `jev-latest`. |
| LM Studio refuses to load the post-editor model | It is being loaded with a context window too large for the free video memory next to SemIf. Load it again with a smaller context length (for example 8192). |
| Test or Try it says the spend cap is reached | Hosted Jev is priced and the monthly cap is used up, so nothing was sent. Raise or clear the cap in Setup AI, or use a local judge (free, never capped). |
| Test works on the GPU box but not from the app | The server is listening on loopback only. Set `SEMIF_HOST` (and `SEMIF_API_KEY`) as in section 5, and check the box is on the tailnet. |
| The server is very slow and the GPU is idle | It was started with a plain `uv run`, which put the CPU torch wheel back. Reinstall the CUDA wheel (section 3) and start it with `uv run --no-sync`. |

Jev runs deterministic structural checks BEFORE the judge: identical source/target text
(after collapsing whitespace and folding case; same base languages are exempt), inline tags/markup, placeholders (including double braces,
ICU arguments and printf), and number/time parity. Numbers-only text, URLs, codes and
applicable glossary DNT terms are exempt from the untranslated check. Identical single tokens (including acronyms and brands) are advisory MINOR findings,
with or without a glossary, and do not block a QE pass. Identical multi-word text
and other structural failures are MAJOR and block a pass regardless of score or threshold. The judge still runs;
its findings are retained. Cards expose `structural_findings` (dimension, severity, reason)
and `structural_blocked`; the shared rationale names the failure in web and CLI views.
The existing empty-target fast path still skips the judge and fails as empty.
The `qe_jev_enabled=0` kill switch disables judge calls, not structural checks.
A disabled, unavailable or unconfident judge has no score; a structural block
still routes to review and says 'judge did not score'. Lab records structural
blocks as 0 with the reason, preserving the judge score separately; an unscored
card without a block stays not applicable. Judge provenance is judge-wrappers/2.
Number and time parity keeps the number checker's own grade. A real mismatch (a changed
value such as 5 -> 50, a changed time such as 6:45 -> 7:45, an AM/PM swap) is MAJOR and
blocks. Two forms are advisory: a dot-format time that equals the source time (Finnish
klo 18.45, German 18.45 Uhr, Danish and Norwegian dot times) and a number from 0 to 10
written as a word (2 -> deux) in English, French, German, Spanish, Italian, Portuguese,
Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Russian, Chinese or Japanese. They
are listed in `structural_findings` for review but do not block a QE pass. Every other
number finding is MAJOR and blocks, including a dropped or added number, a wrong dot
time, digits regrouped into different numbers (1-10 -> 110) and a rendering that is
ambiguous without a locale convention. Jev also reads what a number means, and each of
these is MAJOR. A clock time dropped from the target blocks (hour words such as cinq
heures et demie, an hour-only form such as 20 heures, and Chinese or Japanese clock
times are read as the time). A date whose day, month or year changed blocks: the order is
judged for the target locale, so 3/14/2026 -> 14.03.2026 is correct and
3/14/2026 -> 15.03.2026 is not;
a dot date is day first, ISO dates and Chinese, Japanese and Korean written dates are
read the same way, a date written out in words passes when its day and year match, and a
bare en source locale with both numbers 12 or below leaves both orders open. A lost minus sign blocks (a
hyphen-minus, U+2212, an en dash or a parenthesised currency amount is negative, and a
word such as minus or moins stands in for the sign). A swapped or dropped currency
blocks ($, USD, US$ and dollars are one currency; $ -> EUR is not; placement and spacing
per locale do not matter). A minus the target adds is not judged. An AM/PM marker
counts only on hours 1 to 12, and a bare lowercase am after a German or Dutch time is
the word for on the, not a meridiem. In scripts written without word spaces (Chinese,
Japanese, Thai, Lao, Khmer, Myanmar) a run is one token, so identical text is advisory,
only while it is at most 8 characters after trailing punctuation (ASCII or full-width) is
removed and holds no sentence punctuation inside; a longer or internally punctuated run is
multi-word text and a copy of it is MAJOR. So the short word with a closing mark and the
same word without it are treated alike. HTML void elements (br, hr, img, input and the
like) are one tag kind whether written <br>, <br/> or <br />.
Time parity accepts 17 h 30 and 5:30 p. m.; it abstains when either side has no
parseable time. ICU nested argument names are sets across branches. Markup parity
compares element names and opening/closing/self-closing kind, not attribute values.
Meaning promotion runs BEFORE the low-confidence skip: a meaning signal at its floor
lifts accuracy none, minor or unsure to major. Confident higher severity is retained.
Auto-fix targets confident major dimensions even alongside unsure dimensions, which are
left unchanged and flagged for review. A better rewrite can be kept for review with the
baseline unsure dimensions still flagged; it cannot pass. New unsure dimensions prevent
keeping an attempt. API and MCP return the same cards and flags as web and CLI.
